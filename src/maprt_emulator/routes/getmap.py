"""``POST /integration/GetMap`` — spec §5.2.6, ``IClient.GetMap``.

The odd one out. Every other endpoint answers with the JSON envelope; this one's *success* body
is **streaming plain text**, sent as the computation progresses::

    37,36                  <- couch_samples,gantry_samples
    90,180,1               <- couch,gantry,isOK  (1 = clear, 0 = collision)
    85,180,1
    ...
    DONE

Its *error* body is still the envelope, which is why a client has to sniff the body before
parsing it as a map.

Two consequences of streaming that are worth noticing:

* **Every error has to be decided before the first byte goes out.** Once a response has begun
  streaming its status code is already sent and cannot be changed. So the licence check, the
  surface lookup and the room lookup all happen up front, and nothing below the first ``yield``
  is allowed to fail.
* A real appliance takes **minutes** for a high-resolution map (§4.2.1.3) and reports progress
  as it goes. That is why the C# client takes an ``IProgress<int>``, and why a client with a
  five-second timeout will conclude a working appliance is broken.

**With packs loaded** (:func:`resolve_from_packs`), in order:

1. the map recorded for exactly this request (:func:`maprt_emulator.library.request_key`), byte for
   byte — source ``recorded``;
2. an error recorded for exactly this request;
3. the emulator's own errors: an unknown surface, or one listed but never fetched (rule A: there's
   no mesh to make a map from), is ``SurfaceNotFound``; a room not in the union is
   ``TreatmentRoomNotFound``;
4. **a recorded map of the requested resolution, chosen for this request but recorded for another**
   — real appliance output, but **not this request's data** — source ``random-recorded``;
5. the model's map, computed from the recorded mesh — source ``synthetic``.

``--map-source recorded`` allows only 1 (and 2–3); ``synthetic`` skips 1 and 4. The source is in the
log line, and in ``X-Emulator-Map-Source`` with ``--source-headers``.
"""

import asyncio
import logging

import numpy as np
from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse

from maprt_emulator import envelope, mapgen, replies
from maprt_emulator.auth import require_token
from maprt_emulator.library import GETMAP, request_key, serves_packs
from maprt_emulator.schemas import GetMapRequest

logger = logging.getLogger("maprt_emulator.routes.getmap")

router = APIRouter(prefix="/integration", tags=["integration"],
                   dependencies=[Depends(require_token)])

#: Triples per streamed chunk. Small enough that the body visibly arrives in pieces, large enough
#: that a 65,160-triple map is not 65,160 separate writes.
CHUNK_TRIPLES = 400


@router.post("/GetMap", summary="Compute a clearance map (streaming text, not the envelope)",
             responses={**replies.MAP_REPLY, **replies.BAD_REQUEST})
async def get_map(body: GetMapRequest, request: Request):
    app = request.app

    # --- everything that can fail has to fail here, before streaming starts ------------------

    if not app.state.scenarios.is_licensed("map"):
        raise envelope.MapRTFailure(envelope.FEATURE_NOT_LICENSED)

    if serves_packs(app):
        resolved = await run_in_threadpool(resolve_from_packs, app, body)
        if isinstance(resolved, JSONResponse):
            return resolved
        return _stream(app, body, *resolved)

    surface = app.state.catalog.surface(body.patient_surface_id)
    if surface is None:
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)

    if app.state.catalog.room_by_id(body.treatment_room_id) is None:
        # What the appliance sends (recorded): TreatmentRoomNotFound, typed as a plan error. The
        # usual mistake behind it is passing a room's machineName where its id belongs.
        raise envelope.MapRTFailure(envelope.ROOM_NOT_FOUND)

    text, source = await run_in_threadpool(app.state.map_cache.resolve_text, app, body)
    return _stream(app, body, text, source)


def resolve_from_packs(app, body):
    """``(map text, source)`` for a request with packs loaded, or the :class:`JSONResponse` of a
    recorded error. Raises :class:`envelope.MapRTFailure` for the emulator's own errors. In a
    worker thread: a synthetic map takes about a second."""
    library, settings = app.state.library, app.state.settings
    key = request_key(body)
    surface_id = body.patient_surface_id

    if settings.map_source != "synthetic" and key in library.maps:
        return library.maps[key].text.decode("latin-1"), "recorded"
    recorded_error = library.errors.get((GETMAP, key))
    if recorded_error is not None:
        return JSONResponse(recorded_error.body)

    if surface_id in library.unfetched:
        logger.warning("GetMap surface %s: %s; no map was recorded for this request", surface_id,
                       library.unfetched[surface_id])
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)
    if surface_id not in library.surfaces:
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)
    if body.treatment_room_id not in library.rooms:
        raise envelope.MapRTFailure(envelope.ROOM_NOT_FOUND)

    if settings.map_source == "recorded":
        raise envelope.MapRTFailure(
            envelope.NO_RECORDED_MAP,
            detail="--map-source recorded is set and no map was recorded for this request.")
    if settings.map_source != "synthetic":
        other = library.random_map(body.high_resolution, key, settings.seed)
        if other is not None:
            logger.warning("GetMap: no map was recorded for this request; serving one recorded for another "
                           "(pack %s, %s). It is real appliance output but NOT this request's data.",
                           other.pack, other.stem)
            return other.text.decode("latin-1"), "random-recorded"
    return app.state.map_cache.text_for(body, settings), "synthetic"


def _stream(app, body, text, source):
    settings = app.state.settings

    # --- from here on, only bytes ------------------------------------------------------------

    logger.info("GetMap surface=%s room=%s high_res=%s -> %s",
                body.patient_surface_id[:8], body.treatment_room_id[:8],
                body.high_resolution, source)

    # No media_type: the appliance's map reply carries no Content-Type at all (recorded), which is
    # one more reason a client has to sniff the body rather than trust a header.
    #
    # The source is in the log line above. The header is not a MapRT header, so it goes out only
    # with --source-headers: a client must not find one by default that an appliance never sends.
    headers = {"X-Emulator-Map-Source": source} if settings.source_headers else None
    return StreamingResponse(stream_map_body(text, settings, app.state.scenarios), headers=headers)


class MapCache:
    """Computed maps, keyed by the request fields that affect the result.

    Students re-run the same cell, and a high-resolution map costs about a second of CPU. The
    cache also makes the emulator deterministic in a way that is easy to demonstrate: ask twice,
    get byte-identical answers.

    ``shuffle_map`` deliberately opts out, because a shuffled map that came back identical the
    second time would not be shuffled in any useful sense.
    """

    def __init__(self, catalog, maxsize=32, library=None):
        self._catalog = catalog
        #: With packs loaded, meshes come from their recorded surfaces instead of the catalog.
        self._library = library
        self._surfaces = {}
        self._maps = {}
        self._maxsize = maxsize

    def resolve_text(self, app, body):
        """``(body_text, source)`` for a request: a recorded map if one applies, else computed.

        Called in a worker thread — computing a high-resolution map takes about a second, long
        enough to stall the event loop and make every other request wait behind it.
        """
        settings = app.state.settings

        if settings.map_source != "synthetic":
            recording, source = app.state.maps.resolve(body)
            if recording is not None:
                return recording.text(), source
            if settings.map_source == "recorded":
                # The point of --map-source recorded is "only ever show real data", so a missing
                # recording has to be loud rather than quietly filled in by the model.
                raise envelope.MapRTFailure(
                    envelope.NO_RECORDED_MAP,
                    detail="--map-source recorded is set and no recorded map matches this "
                           "surface, room and resolution.",
                )

        return self.text_for(body, settings), "synthetic"

    def surface_model(self, surface_id):
        """The decimated :class:`mapgen.SurfaceModel`, built once per surface."""
        if surface_id not in self._surfaces:
            if self._library is not None and self._library.packs:
                data = self._library.surface_obj(surface_id)
            else:
                data = self._catalog.surface(surface_id).path.read_bytes()
            self._surfaces[surface_id] = mapgen.SurfaceModel.from_obj_bytes(data, surface_id)
        return self._surfaces[surface_id]

    @staticmethod
    def _key(body, settings):
        return (body.patient_surface_id, body.treatment_room_id, body.patient_position,
                round(body.couch_buffer, 3), round(body.patient_buffer, 3),
                tuple(round(v, 3) for v in body.isocenter.as_tuple()),
                body.high_resolution, settings.seed)

    def text_for(self, body, settings):
        """The computed map body for a request, cached.

        Always in canonical order. Shuffling happens when the body is streamed, not here — see
        :func:`stream_map_body`.
        """
        key = self._key(body, settings)
        if key in self._maps:
            return self._maps[key]

        model = self.surface_model(body.patient_surface_id)
        couch, gantry, safe = mapgen.compute_map(model, body, seed=settings.seed)
        text = mapgen.map_text(couch, gantry, safe)

        if len(self._maps) >= self._maxsize:
            self._maps.pop(next(iter(self._maps)))
        self._maps[key] = text
        return text


async def stream_map_body(text, settings, scenarios=None):
    """Yield a map body in chunks over roughly ``settings.map_seconds``.

    Chunked and paced so the response genuinely arrives progressively, as a real appliance's
    does. A client that reads the whole body before parsing sees no difference; one that reads
    line by line can show progress, which is the point of the format.

    The ``truncate-map`` scenario stops early — before the ``DONE`` line — which is the only
    way to show a client why that terminator is worth checking.
    """
    lines = text.splitlines(keepends=True)
    header, rest = lines[0], lines[1:]

    if scenarios is not None and scenarios.shuffles_map:
        # Order is a property of the bytes on the wire, so it is applied here rather than when
        # the map is computed. Applied at generation time it silently did nothing to a *recorded*
        # map — which is the one the shipped data serves, so the scenario lied in the one case a
        # student was most likely to try. DONE is held back and re-appended so the terminator
        # stays last; it is a marker, not a triple.
        terminator = [line for line in rest if line.strip() == "DONE"]
        triples = [line for line in rest if line.strip() and line.strip() != "DONE"]
        np.random.default_rng().shuffle(triples)
        rest = triples + terminator

    if scenarios is not None and scenarios.truncates_map:
        # Drop DONE and the tail: what a dropped connection mid-computation looks like.
        #
        # The failure this exposes is nastier than "some angles are missing". The parser infers
        # the gantry axis from the values it actually sees, so a half-delivered map parses to a
        # half-height array — (180, 185) instead of (360, 185) — and code that indexes it by
        # gantry row then reads a completely different angle than it asked for, or raises an
        # IndexError far from the cause. Nothing in the data says anything is wrong. That is
        # what DONE is for.
        rest = rest[:max(1, len(rest) // 2)]

    yield header

    chunks = [rest[i:i + CHUNK_TRIPLES] for i in range(0, len(rest), CHUNK_TRIPLES)]
    pause = (settings.map_seconds / len(chunks)) if chunks and settings.map_seconds > 0 else 0.0

    for chunk in chunks:
        if pause:
            await asyncio.sleep(pause)
        yield "".join(chunk)
