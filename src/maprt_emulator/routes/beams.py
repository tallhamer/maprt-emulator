"""``POST /integration/GetBeamDeliveryStatus`` — spec §5.2.7, ``IClient.GetBeamDeliveryStatus``.

Answers, per beam, whether the beams in the request can be delivered safely (§4.2.1.2). It checks
the clearance at each control point it is sent (§2.3.2), so it is much quicker than computing a
full clearance map: a handful of angles instead of tens of thousands.

That speed has a price a client has to understand: **the server only checks what you send it.**
A beam carrying two control points is two angles checked, not the arc between them. Send a
360-degree arc as its start and stop and you will be told it is safe while it sweeps straight
through a collision. Nothing is wrong with the endpoint — that is what it does — but it is the
difference between this and a client-side sweep over a full clearance map, and it is the kind of
difference that matters.

Separately licensed (§2.3.3), so ``FeatureNotLicensed`` is a normal case to handle.

**With packs loaded** (:func:`resolve_from_packs`), in order:

1. the verdict recorded for exactly this request (:func:`maprt_emulator.library.request_key`), as
   recorded — source ``recorded``;
2. an error recorded for exactly this request;
3. the emulator's own errors: an unknown surface, or one listed but never fetched (rule A: there's
   no mesh to judge it by), is ``SurfaceNotFound``; a room not in the union is
   ``TreatmentRoomNotFound``;
4. **derived** from the high-resolution map recorded for the same surface, room, position, buffers
   and isocenter: a beam is safe when every control point, rounded to the whole degree, is clear on
   that map. Only when every control point of every beam is on the map — source ``derived``;
5. the model's verdicts, from the recorded mesh — source ``synthetic``.

The source is in the log line, and in ``X-Emulator-Beam-Source`` with ``--source-headers``.
"""

import logging
import math

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from maprt_emulator import envelope, mapgen, replies
from maprt_emulator.auth import require_token
from maprt_emulator.library import BEAMS, request_key, serves_packs
from maprt_emulator.schemas import GetBeamDeliveryStatusRequest, GetMapRequest

logger = logging.getLogger("maprt_emulator.routes.beams")

router = APIRouter(prefix="/integration", tags=["integration"],
                   dependencies=[Depends(require_token)])


def resolve_clearance_target(request, body, feature):
    """The surface model for a clearance request, or raise the right MapRT failure.

    Kept apart from the route so the checks read in the order a real appliance applies them: the
    licence, then the surface, then the room.
    """
    app = request.app

    if not app.state.scenarios.is_licensed(feature):
        raise envelope.MapRTFailure(envelope.FEATURE_NOT_LICENSED)

    if app.state.catalog.surface(body.patient_surface_id) is None:
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)

    if app.state.catalog.room_by_id(body.treatment_room_id) is None:
        raise envelope.MapRTFailure(envelope.ROOM_NOT_FOUND)

    return app.state.map_cache.surface_model(body.patient_surface_id)


@router.post("/GetBeamDeliveryStatus", summary="Assess whether each beam can be safely delivered",
             responses={**replies.json_reply(replies.BeamStatusReply), **replies.BAD_REQUEST})
async def get_beam_delivery_status(body: GetBeamDeliveryStatusRequest, request: Request):
    if serves_packs(request.app):
        if not request.app.state.scenarios.is_licensed("beams"):
            raise envelope.MapRTFailure(envelope.FEATURE_NOT_LICENSED)
        reply, source = await run_in_threadpool(resolve_from_packs, request.app, body)
        logger.info("GetBeamDeliveryStatus %d beam(s) -> %s", len(body.beam_list), source)
        headers = {"X-Emulator-Beam-Source": source} if request.app.state.settings.source_headers else None
        return JSONResponse(reply, headers=headers)

    surface = resolve_clearance_target(request, body, "beams")
    seed = request.app.state.settings.seed

    # The verdicts come from mapgen.is_safe_at, the same function GetMap evaluates over its grid.
    # So a client that downloads the map and checks the same angles itself gets the same answers,
    # which is a property worth being able to rely on rather than a happy accident.
    verdicts = await run_in_threadpool(mapgen.beam_verdicts, surface, body, seed)

    logger.info("GetBeamDeliveryStatus %d beam(s) -> %d safe, synthetic",
                len(verdicts), sum(1 for _n, safe in verdicts if safe))

    reply = envelope.ok({
        "statuses": [{"beamNumber": number, "isSafe": safe} for number, safe in verdicts],
    })
    # Not a MapRT header; only with --source-headers, as for GetMap. Without packs, every verdict
    # is computed.
    headers = {"X-Emulator-Beam-Source": "synthetic"} if request.app.state.settings.source_headers else None
    return JSONResponse(reply, headers=headers)


def resolve_from_packs(app, body):
    """``(reply body, source)`` for a request with packs loaded. Raises
    :class:`envelope.MapRTFailure` for the emulator's own errors. In a worker thread: a synthetic
    verdict means building the surface model."""
    library = app.state.library
    key = request_key(body)
    surface_id = body.patient_surface_id

    if key in library.verdicts:
        return library.verdicts[key].body, "recorded"
    recorded_error = library.errors.get((BEAMS, key))
    if recorded_error is not None:
        return recorded_error.body, "recorded"

    if surface_id in library.unfetched:
        logger.warning("GetBeamDeliveryStatus surface %s: %s; no verdict was recorded for this request",
                       surface_id, library.unfetched[surface_id])
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)
    if surface_id not in library.surfaces:
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)
    if body.treatment_room_id not in library.rooms:
        raise envelope.MapRTFailure(envelope.ROOM_NOT_FOUND)

    derived = derive_verdicts(library, body)
    if derived is not None:
        return _statuses(derived), "derived"
    model = app.state.map_cache.surface_model(surface_id)
    return _statuses(mapgen.beam_verdicts(model, body, app.state.settings.seed)), "synthetic"


def derive_verdicts(library, body):
    """``[(beam_number, is_safe), …]`` from the high-resolution map recorded for this request's
    surface, room, position, buffers and isocenter, or ``None`` when there is none or a control
    point isn't on it (a couch angle outside the map's range, say)."""
    common = body.model_dump(by_alias=True, exclude={"beam_list"})
    recorded = library.maps.get(request_key(GetMapRequest.model_validate({**common, "HighResolution": True})))
    if recorded is None:
        return None
    clear = _map_cells(recorded.text)
    verdicts = []
    for beam in body.beam_list:
        cells = [(_degree(point.couch_angle), _degree(point.gantry_angle)) for point in beam.control_points]
        if any(cell not in clear for cell in cells):
            return None
        # No control points is no evidence of safety, as in mapgen.beam_verdicts.
        verdicts.append((beam.beam_number, bool(cells) and all(clear[cell] for cell in cells)))
    return verdicts


def _map_cells(text):
    """``{(couch, gantry): is_clear}`` from a GetMap body's triples."""
    cells = {}
    for line in text.decode("latin-1").splitlines()[1:]:
        parts = line.split(",")
        if len(parts) == 3:
            cells[(_degree(float(parts[0])), _degree(float(parts[1])))] = parts[2].strip() == "1"
    return cells


def _degree(angle):
    """An angle as the whole degree the map is sampled at, in [0, 360)."""
    return math.floor(float(angle) + 0.5) % 360


def _statuses(verdicts):
    return envelope.ok({"statuses": [{"beamNumber": number, "isSafe": safe} for number, safe in verdicts]})
