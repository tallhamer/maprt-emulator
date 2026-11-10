"""The catalog the emulator serves: treatment rooms, patients, surfaces.

Everything comes out of a folder, so adding data is a file drop and a line of JSON — no code
change. The layout::

    data/MapRT/rooms.json         [{"id", "machineName", "name", "coordinateSystem"}, …]
    data/MapRT/patients.json      {"<patientId>": [{"id", "timeStamp", "timeZone"}, …], …}
    data/MapRT/surfaces/<id>.obj  the mesh served for that surface id
    data/MapRT/local/             an optional overlay, git-ignored — see below

``data/MapRT/local/`` shadows ``data/MapRT/``: a ``rooms.json`` or ``patients.json`` there replaces the
public one, and surfaces there are added to (and win over) the public ones. That is where real
exported patient surfaces go on a machine that has them, so patient-derived data can never
become a commit candidate.
"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path

#: What ``/integration/rooms/{machineName}`` returns when no room matches (spec §5.2.4). Not an
#: error — an all-zeros GUID. A client that only checks for an error will sail straight past it.
EMPTY_GUID = "00000000-0000-0000-0000-000000000000"


@dataclass(frozen=True)
class Room:
    """One treatment room configuration.

    ``machine_name`` is the field you look a room up by and the field a human recognises. It is
    **not unique** — a real site commonly has several room configurations on one machine (one per
    accessory set, say), all carrying the same machine name. ``id`` is the GUID, and the GUID is
    what GetMap and GetBeamDeliveryStatus actually key on.
    """

    id: str
    machine_name: str
    name: str
    coordinate_system: str

    def to_json(self):
        return {
            "id": self.id,
            "machineName": self.machine_name,
            "name": self.name,
            "coordinateSystem": self.coordinate_system,
        }


@dataclass(frozen=True)
class Surface:
    """One acquired patient surface.

    ``timestamp`` is UTC and ``timezone`` names the zone it was acquired in, which is how the
    appliance reports it (§3.1) — the pair, not a single offset-bearing string.
    """

    id: str
    patient_id: str
    timestamp: str
    timezone: str
    path: Path

    def to_json(self):
        return {"id": self.id, "timeStamp": self.timestamp, "timeZone": self.timezone}


class CatalogError(Exception):
    """The data folder is missing or malformed. Raised at startup, never mid-request."""


class Catalog:
    """The loaded contents of a data folder."""

    def __init__(self, rooms, surfaces, patient_ids, data_dir):
        self.data_dir = data_dir
        self.rooms = list(rooms)
        self._surfaces = list(surfaces)
        self._by_id = {surface.id: surface for surface in self._surfaces}
        # Seeded from the full patient list, not from the surfaces, so a patient with an empty
        # surface list survives loading. That patient is the difference between "I know them and
        # they have nothing" (an empty list, success) and "I've never heard of them" (an error),
        # which is a distinction a client has to handle and so one the emulator has to offer.
        self._by_patient = {patient_id: [] for patient_id in patient_ids}
        for surface in self._surfaces:
            self._by_patient.setdefault(surface.patient_id, []).append(surface)
        # Newest first, which is the order a planner wants: the most recent surface of a patient
        # is the one that reflects how they actually set up.
        for entries in self._by_patient.values():
            entries.sort(key=lambda s: s.timestamp, reverse=True)

    # -- loading ------------------------------------------------------------------------------

    @classmethod
    def load(cls, data_dir):
        """Read ``data_dir``, with ``data_dir/local`` overlaid on top."""
        data_dir = Path(data_dir)
        if not data_dir.is_dir():
            raise CatalogError(f"No data directory at {data_dir}. Pass --data-dir to point at one.")

        overlay = data_dir / "local"

        rooms = cls._load_rooms(_prefer(overlay / "rooms.json", data_dir / "rooms.json"))
        patients = _prefer(overlay / "patients.json", data_dir / "patients.json")
        surfaces, patient_ids = cls._load_surfaces(
            patients, [overlay / "surfaces", data_dir / "surfaces"])
        return cls(rooms, surfaces, patient_ids, data_dir)

    @staticmethod
    def _load_rooms(path):
        if path is None:
            raise CatalogError("No rooms.json found in the data directory.")
        entries = json.loads(path.read_text(encoding="utf-8"))
        rooms = []
        for entry in entries:
            try:
                rooms.append(Room(id=entry["id"], machine_name=entry["machineName"],
                                  name=entry["name"], coordinate_system=entry["coordinateSystem"]))
            except KeyError as exc:
                raise CatalogError(f"A room in {path} is missing {exc}.") from exc
        if not rooms:
            raise CatalogError(f"{path} lists no treatment rooms.")
        return rooms

    @staticmethod
    def _load_surfaces(patients_path, surface_dirs):
        if patients_path is None:
            raise CatalogError("No patients.json found in the data directory.")
        catalog = json.loads(patients_path.read_text(encoding="utf-8"))
        surfaces = []
        for patient_id, entries in catalog.items():
            for entry in entries:
                try:
                    surface_id = entry["id"]
                except KeyError as exc:
                    raise CatalogError(f"A surface of {patient_id} in {patients_path} has no id.") from exc
                # The overlay directory is searched first, so a real surface dropped into
                # data/MapRT/local/surfaces/ replaces the synthetic one of the same id.
                path = _prefer(*(d / f"{surface_id}.obj" for d in surface_dirs))
                if path is None:
                    searched = ", ".join(str(d) for d in surface_dirs)
                    raise CatalogError(
                        f"{patients_path} lists surface {surface_id} for patient {patient_id}, "
                        f"but no {surface_id}.obj exists in: {searched}"
                    )
                surfaces.append(Surface(
                    id=surface_id,
                    patient_id=patient_id,
                    timestamp=entry.get("timeStamp", "1970-01-01T00:00:00"),
                    timezone=entry.get("timeZone", "UTC"),
                    path=path,
                ))
        return surfaces, set(catalog)

    @property
    def known_patients(self):
        """Every patient id in patients.json, including those with no surfaces."""
        return set(self._by_patient)

    def knows_patient(self, patient_id):
        return patient_id in self._by_patient

    # -- lookups ------------------------------------------------------------------------------

    @property
    def surfaces(self):
        """Every surface in the catalog, across all patients."""
        return list(self._surfaces)

    def rooms_json(self):
        return [room.to_json() for room in self.rooms]

    def rooms_by_machine_name(self, machine_name):
        """Every room with this machine name — 0, 1, or (the interesting case) more than one."""
        return [room for room in self.rooms if room.machine_name == machine_name]

    def room_ids_by_machine_name(self, machine_name):
        return [room.id for room in self.rooms_by_machine_name(machine_name)]

    def room_by_id(self, room_id):
        return next((room for room in self.rooms if room.id == room_id), None)

    def surfaces_for(self, patient_id):
        """This patient's surfaces, newest first. Empty list for a patient with none."""
        return list(self._by_patient.get(patient_id, ()))

    def surface(self, surface_id):
        return self._by_id.get(surface_id)

    def surface_data_uri(self, surface_id):
        """The surface as the API sends it: a base64 ``data:`` URI of the raw ``.obj`` bytes."""
        surface = self.surface(surface_id)
        if surface is None:
            return None
        encoded = base64.b64encode(surface.path.read_bytes()).decode("ascii")
        return f"data:application/octet-stream;base64,{encoded}"

    def summary(self):
        """One line for the startup banner."""
        return (f"{len(self._by_patient)} patients · {len(self._surfaces)} surfaces · "
                f"{len(self.rooms)} rooms")


def _prefer(*paths):
    """The first path that exists, or None. How the ``data/MapRT/local`` overlay shadows ``data/MapRT``."""
    for path in paths:
        if path is not None and path.is_file():
            return path
    return None
