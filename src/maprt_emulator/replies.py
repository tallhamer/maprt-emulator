"""The reply shapes a real appliance sends, as recorded, for ``/docs`` and the tests.

Nothing here builds a reply: the routes return plain dicts from :mod:`maprt_emulator.envelope`.
These models only **describe** them, so ``/docs`` shows a user exactly the keys an appliance sends,
and the tests can check every reply against the same description. Each model forbids extra keys,
so a reply carrying anything an appliance does not send fails validation.

Two envelopes, both at HTTP 200 (recorded, Saturn/1.3.217.2, API 2.0.0):

* success: ``data``, ``errorData`` (null), ``success`` (true), ``errorCode`` (null),
  ``errorCodeType`` (``"NoErrorCodes"``);
* failure: the same **without** ``data``, with ``success`` false and a code.

GetMap's success is not an envelope at all: CRLF text, with no ``Content-Type``.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, create_model


class _Exact(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Room(_Exact):
    id: str
    machineName: str
    name: str
    coordinateSystem: str


class SurfaceListItem(_Exact):
    id: str
    #: UTC, e.g. ``"2026-10-21T22:32:51.1"``; the zone it was acquired in is ``timeZone``.
    timeStamp: str
    timeZone: str


class BeamStatus(_Exact):
    beamNumber: int
    isSafe: bool


class BeamStatuses(_Exact):
    statuses: list[BeamStatus]


class ErrorReply(_Exact):
    """A negative result. Note there is no ``data`` key, and ``errorData`` is always null."""

    errorData: None
    success: Literal[False]
    errorCode: str
    errorCodeType: str


def _success(name, data_type, doc):
    return create_model(
        name, __base__=_Exact, __doc__=doc,
        data=(data_type, ...),
        errorData=(None, ...),
        success=(Literal[True], ...),
        errorCode=(None, ...),
        errorCodeType=(Literal["NoErrorCodes"], ...),
    )


PingReply = _success("PingReply", str, "The API version, e.g. ``\"2.0.0\"``.")
RoomsReply = _success("RoomsReply", list[Room], "Every treatment room configuration.")
RoomIdReply = _success("RoomIdReply", str,
                       "The room's GUID; the all-zeros GUID when no room has that machine name.")
SurfaceListReply = _success("SurfaceListReply", list[SurfaceListItem],
                            "The patient's surfaces, newest first. Empty for a patient with none.")
SurfaceReply = _success("SurfaceReply", str,
                        "The mesh as a data URI: ``data:application/octet-stream;base64,<OBJ>``.")
BeamStatusReply = _success("BeamStatusReply", BeamStatuses, "One verdict per beam, in request order.")

#: An example GetMap body: the grid size, then ``couch,gantry,isOK`` lines, then ``DONE``.
MAP_EXAMPLE = "37,36\r\n90,180,1\r\n85,180,1\r\n80,180,0\r\nDONE\r\n"


def json_reply(success_model):
    """``responses=`` for a JSON route: HTTP 200 carries either envelope."""
    return {200: {"model": success_model | ErrorReply,
                  "description": "The success envelope, or a negative result (also HTTP 200)."}}


#: ``responses=`` for the two POST routes, which also reject a malformed body with a 400.
BAD_REQUEST = {400: {"model": ErrorReply, "description": "A malformed request body."}}

#: ``responses=`` for GetMap: map text on success, the error envelope otherwise.
MAP_REPLY = {200: {
    "model": ErrorReply,
    "description": "The map as CRLF text (an appliance sends no Content-Type), or a negative "
                   "result as JSON.",
    "content": {"text/plain": {"schema": {"type": "string"}, "example": MAP_EXAMPLE}},
}}
