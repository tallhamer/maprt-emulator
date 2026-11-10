"""Recorded clearance maps: serving a real GetMap response body back out of a folder.

The synthetic generator in :mod:`maprt_emulator.mapgen` is plausible but invented. When you have a
*real* appliance response, you can drop it in here and the emulator will serve the real bytes —
which is what you want for a live demo, and the only way the workshop shows anyone a map that
actually came off a linac.

    data/MapRT/maps/index.json   [{"file", "surfaceId", "roomId", "patientPosition",
                                   "couchBuffer", "patientBuffer", "isocenter": [x, y, z],
                                   "highResolution": bool, "note": "..."}, …]
    data/MapRT/maps/<name>.txt   the raw GetMap response body, verbatim, including the DONE line

``data/MapRT/local/maps/`` overlays this, git-ignored, which is where patient-derived recordings belong.

Resolution order for a request. The server log names the one that answered, and so does the
``X-Emulator-Map-Source`` response header when ``--source-headers`` is on:

``recorded``
    Every keyed field matches a recording. You are looking at real bytes.
``recorded-nearest``
    Same surface, room and resolution, but a different isocenter — the closest recording is
    served. Keeps a notebook working when a student types an arbitrary isocenter, at the cost of
    the answer not being for the setup they asked about. Hence the header.
``synthetic``
    No recording applies, so the model computed one.

The source is reported because "is this real?" is a question a student should always be able to
answer, and because a nearest-match is a quietly wrong answer if nothing says so. It is a header
only on request, because an appliance sends no such header.
"""

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("maprt_emulator.maps")

#: Isocenter components are matched to the nearest mm, buffers to a tenth of a mm. Exact float
#: equality would make an exact match essentially unreachable from a hand-typed notebook cell.
ISOCENTER_TOLERANCE_MM = 0.5
BUFFER_TOLERANCE_MM = 0.05


class MapLibraryError(Exception):
    """A recorded-map folder that cannot be loaded. Raised at startup, never mid-request."""


@dataclass(frozen=True)
class RecordedMap:
    path: Path
    surface_id: str
    room_id: str
    patient_position: str
    couch_buffer: float
    patient_buffer: float
    isocenter: tuple
    high_resolution: bool
    note: str = ""

    def describes_same_setup(self, request):
        """Whether this recording is for the same surface, room and resolution as ``request``.

        Deliberately ignores the isocenter and the buffers — this is the test for whether the
        recording is a candidate for a *nearest* match.
        """
        return (self.surface_id == request.patient_surface_id
                and self.room_id == request.treatment_room_id
                and self.high_resolution == request.high_resolution)

    def matches(self, request):
        """Whether this recording is for exactly the setup ``request`` asks about."""
        if not self.describes_same_setup(request):
            return False
        if self.patient_position != request.patient_position:
            return False
        if abs(self.couch_buffer - request.couch_buffer) > BUFFER_TOLERANCE_MM:
            return False
        if abs(self.patient_buffer - request.patient_buffer) > BUFFER_TOLERANCE_MM:
            return False
        return self.isocenter_distance(request) <= ISOCENTER_TOLERANCE_MM

    def isocenter_distance(self, request):
        requested = request.isocenter.as_tuple()
        return math.dist(self.isocenter, requested)

    def text(self):
        """The recorded response body, verbatim — line endings included.

        ``newline=""`` stops Python translating the appliance's CRLF to LF on the way in.
        """
        with self.path.open(encoding="utf-8", newline="") as handle:
            return handle.read()


class MapLibrary:
    """The recorded maps available to the emulator."""

    def __init__(self, recordings):
        self.recordings = list(recordings)

    def __len__(self):
        return len(self.recordings)

    @classmethod
    def load(cls, data_dir):
        """Read ``data_dir/maps`` with ``data_dir/local/maps`` overlaid. Missing is fine: empty."""
        data_dir = Path(data_dir)
        recordings = []
        # The overlay is read second so that, where both define a recording for the same setup,
        # the local (real) one is found first by `resolve` — see the ordering note there.
        for maps_dir in (data_dir / "local" / "maps", data_dir / "maps"):
            index = maps_dir / "index.json"
            if not index.is_file():
                continue
            try:
                entries = json.loads(index.read_text(encoding="utf-8"))
            except ValueError as exc:
                raise MapLibraryError(f"{index} is not valid JSON: {exc}") from exc
            for entry in entries:
                recordings.append(cls._recording_from(entry, index))
        return cls(recordings)

    @staticmethod
    def _recording_from(entry, index):
        try:
            path = index.parent / entry["file"]
            if not path.is_file():
                raise MapLibraryError(f"{index} lists {entry['file']}, which does not exist.")
            isocenter = tuple(float(v) for v in entry.get("isocenter", (0.0, 0.0, 0.0)))
            if len(isocenter) != 3:
                raise MapLibraryError(f"{index}: isocenter for {entry['file']} is not x, y, z.")
            return RecordedMap(
                path=path,
                surface_id=entry["surfaceId"],
                room_id=entry["roomId"],
                patient_position=str(entry.get("patientPosition", "HFS")).upper(),
                couch_buffer=float(entry.get("couchBuffer", 0.0)),
                patient_buffer=float(entry.get("patientBuffer", 0.0)),
                isocenter=isocenter,
                high_resolution=bool(entry.get("highResolution", False)),
                note=entry.get("note", ""),
            )
        except KeyError as exc:
            raise MapLibraryError(f"A recording in {index} is missing {exc}.") from exc

    def resolve(self, request, allow_nearest=True):
        """``(recording, source)`` for ``request``, or ``(None, None)``.

        ``source`` is ``"recorded"`` or ``"recorded-nearest"``. The first exact match wins, and
        because the local overlay is loaded first, a real recording beats a shipped one for the
        same setup.
        """
        for recording in self.recordings:
            if recording.matches(request):
                return recording, "recorded"

        if not allow_nearest:
            return None, None

        candidates = [r for r in self.recordings if r.describes_same_setup(request)]
        if not candidates:
            return None, None

        nearest = min(candidates, key=lambda r: r.isocenter_distance(request))
        logger.info(
            "No recorded map for isocenter %s; serving %s, recorded at %s (%.0f mm away).",
            request.isocenter.as_tuple(), nearest.path.name, nearest.isocenter,
            nearest.isocenter_distance(request),
        )
        return nearest, "recorded-nearest"

    def summary(self):
        count = len(self.recordings)
        return f"{count} recorded map{'' if count == 1 else 's'}"
