"""Patient and surface endpoints — spec §5.2.2 and §5.2.3.

Getting a mesh takes two calls: list the patient's surfaces, then fetch one by its id.

Three things about the payload that catch people out:

* The mesh arrives as a **base64 data URI** in a JSON string, not as a binary body. You split on
  the last comma and decode the tail.
* It is in **NATIVE (couch) coordinates** and carries **no patient-position metadata** (§3.2.1).
  The bytes do not tell you whether this person was head-first or feet-first; you have to know.
* Surface data is **separately licensed** (§2.3.3), so ``FeatureNotLicensed`` is a normal thing
  to handle here, not an exotic failure.

An unknown *patient* and a known patient with *no surfaces* are different answers, and a client
has to treat them differently — see the handler below.

**With packs loaded**, both answer from the packs (:class:`maprt_emulator.library.PackLibrary`),
which replace the catalog's patients and surfaces:

* a patient's list is the union of their recorded lists, every surface as recorded — **including
  one that was listed but never fetched**, so the list matches the recording;
* a fetched surface is served as its recorded reply;
* a surface that was listed but never fetched answers ``SurfaceNotFound``, as a real appliance
  answers an unknown surface. A real appliance would send the mesh; with none recorded, an error
  is the only honest answer. It's logged, and named in ``X-Emulator-Surface-Source: unfetched``
  with ``--source-headers``;
* otherwise a recorded error for that patient or surface, if there is one (a recorded success
  anywhere beats it), else the emulator's own.
"""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from maprt_emulator import envelope, replies
from maprt_emulator.auth import require_token
from maprt_emulator.library import PATIENT_SURFACES, SURFACE, serves_packs

logger = logging.getLogger("maprt_emulator.routes.surfaces")

router = APIRouter(prefix="/integration", tags=["integration"],
                   dependencies=[Depends(require_token)])


@router.get("/patients/{patient_id}/surfaces", summary="List a patient's acquired surfaces",
            responses=replies.json_reply(replies.SurfaceListReply))
async def patient_surfaces(patient_id: str, request: Request):
    # Spec §5.2.2: patient ids are DICOM ids, maximum 64 characters. What an appliance does with
    # a longer one is not recorded, so this is the spec's malformed-request 4xx, labelled invented.
    if len(patient_id) > 64:
        raise envelope.MapRTFailure(
            envelope.INVALID_ARGUMENT,
            status_code=400,
            detail=f"patientId is limited to 64 characters; got {len(patient_id)}.",
        )

    if serves_packs(request.app):
        library = request.app.state.library
        if patient_id in library.patients:
            return JSONResponse(library.patient_reply(patient_id))
        recorded = library.errors.get((PATIENT_SURFACES, patient_id))
        if recorded is not None:
            return JSONResponse(recorded.body)
        raise envelope.MapRTFailure(envelope.PATIENT_NOT_FOUND)

    catalog = request.app.state.catalog

    if not catalog.knows_patient(patient_id):
        # An unknown patient is an error — PatientNotFoundInDatabase, as the appliance sends it
        # (recorded) — while a known patient with no surfaces is a success (below).
        raise envelope.MapRTFailure(envelope.PATIENT_NOT_FOUND)

    # A known patient with nothing acquired is a SUCCESS carrying an empty list. Distinguishing
    # the two is the whole point of handling them separately: "we have never seen this person"
    # and "this person has no surface yet" lead to different conversations with a planner.
    return envelope.ok([surface.to_json() for surface in catalog.surfaces_for(patient_id)])


@router.get("/surfaces/{surface_id}", summary="Fetch one surface mesh as base64 OBJ",
            responses=replies.json_reply(replies.SurfaceReply))
async def surface(surface_id: str, request: Request):
    # Through the scenario state, not settings, so both --unlicensed surfaces and the
    # `unlicensed-surfaces` scenario reach here. Checking settings directly would arm a fault
    # that never fires.
    if not request.app.state.scenarios.is_licensed("surfaces"):
        raise envelope.MapRTFailure(envelope.FEATURE_NOT_LICENSED)

    if serves_packs(request.app):
        return _surface_from_packs(surface_id, request)

    data_uri = request.app.state.catalog.surface_data_uri(surface_id)
    if data_uri is None:
        raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)

    return envelope.ok(data_uri)


def _surface_from_packs(surface_id, request):
    library = request.app.state.library
    fetched = library.surfaces.get(surface_id)
    if fetched is not None:
        return JSONResponse(fetched.body)

    unfetched = library.unfetched.get(surface_id)
    if unfetched is not None:
        logger.warning("GET surface %s: %s", surface_id, unfetched)
        headers = {"X-Emulator-Surface-Source": "unfetched"} if request.app.state.settings.source_headers else None
        return JSONResponse(envelope.fail(*envelope.SURFACE_NOT_FOUND), headers=headers)

    recorded = library.errors.get((SURFACE, surface_id))
    if recorded is not None:
        return JSONResponse(recorded.body)
    raise envelope.MapRTFailure(envelope.SURFACE_NOT_FOUND)
