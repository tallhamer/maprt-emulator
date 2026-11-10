"""Treatment-room endpoints — spec §5.2.5 and §5.2.4.

Two ways to ask about rooms, and the difference between them is a lesson:

* ``GET /integration/rooms`` lists every room configuration, each with a GUID **and** a machine
  name.
* ``GET /integration/rooms/{machineName}`` resolves *one* room by its machine name.

The second one looks more convenient and is the one to be careful with. A machine name is not
unique — a site commonly has several room configurations on one machine — so the lookup can
match nothing (an all-zeros GUID, **not** an error) or match several (a ``DuplicateKey`` error at
HTTP 200). Production code lists the rooms once and keys on the GUID thereafter, which is what
GetMap and GetBeamDeliveryStatus want anyway.

**With packs loaded**, both answer from the union of the packs' recorded room lists
(:class:`maprt_emulator.library.PackLibrary`), which replaces the catalog's rooms. A lookup is
answered from that list by the same rules, never from a recorded lookup reply, so a pack whose
capture made no lookup still answers every case.
"""

from fastapi import APIRouter, Depends, Request

from maprt_emulator import envelope, replies
from maprt_emulator.auth import require_token
from maprt_emulator.library import serves_packs
from maprt_emulator.store import EMPTY_GUID

router = APIRouter(prefix="/integration", tags=["integration"],
                   dependencies=[Depends(require_token)])


@router.get("/rooms", summary="List every treatment room configuration",
            responses=replies.json_reply(replies.RoomsReply))
async def all_rooms(request: Request):
    return envelope.ok(_rooms(request).rooms_json())


@router.get("/rooms/{machine_name}", summary="Resolve one treatment room by machine name",
            responses=replies.json_reply(replies.RoomIdReply))
async def single_room(machine_name: str, request: Request):
    # The spec caps machine names at 16 characters (§5.2.4), but the appliance does not enforce
    # it: an over-long name is simply one that matches nothing (recorded). No special case here.
    matches = _rooms(request).room_ids_by_machine_name(machine_name)

    if len(matches) > 1:
        # More than one match is the AmbiguousMatchException of the C# client (§4.2.1.5), which
        # over REST is the DuplicateKey error of §5.3 — at HTTP 200, like every negative result.
        raise envelope.MapRTFailure(envelope.DUPLICATE_KEY)

    if not matches:
        # No match is NOT an error: success, with the all-zeros GUID as the answer. A client that
        # only checks `success` will carry this straight into a GetMap call and get nowhere.
        return envelope.ok(EMPTY_GUID)

    return envelope.ok(matches[0])


def _rooms(request):
    """Where rooms come from: the packs when any are loaded, else the catalog."""
    app = request.app
    return app.state.library if serves_packs(app) else app.state.catalog
