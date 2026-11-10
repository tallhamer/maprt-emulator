"""Every reply has exactly the keys a real appliance sends — no more, no fewer — and ``/docs``
describes exactly that.

The shapes are :mod:`maprt_emulator.replies`, written from Session Recordings of a real appliance
(2026-10-23). Each model forbids extra keys and requires every recorded one, so validating a
reply against it checks both directions. ``/docs`` is generated from the same models, so the
page and the replies can't drift apart without a test here failing.
"""

import json

import pytest
from pydantic import ValidationError

from maprt_emulator import replies

SURFACE = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"
ROOM = "96bbc235-0da9-e983-0783-5973a7d11144"
SETUP = {"PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "PatientPosition": "HFS",
         "CouchBuffer": 0.0, "PatientBuffer": 0.0, "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0}}
BEAMS = {**SETUP, "BeamList": [{"beamNumber": "1", "controlPoints": [
    {"gantryAngle": 0.0, "couchAngle": 0.0, "coordinateSystem": "IEC_61217"}]}]}


@pytest.fixture
def fast_client(client_factory):
    with client_factory(map_seconds=0.0) as c:
        yield c


SUCCESSES = [
    ("get", "/integration/ping", None, replies.PingReply),
    ("get", "/integration/rooms", None, replies.RoomsReply),
    ("get", "/integration/rooms/TrueBeam2", None, replies.RoomIdReply),
    ("get", "/integration/rooms/NoSuchMachine", None, replies.RoomIdReply),
    ("get", "/integration/patients/ANON001/surfaces", None, replies.SurfaceListReply),
    ("get", "/integration/patients/ANON003/surfaces", None, replies.SurfaceListReply),
    ("get", f"/integration/surfaces/{SURFACE}", None, replies.SurfaceReply),
    ("post", "/integration/GetBeamDeliveryStatus", BEAMS, replies.BeamStatusReply),
]

FAILURES = [
    ("get", "/integration/rooms/TrueBeam", None),                      # DuplicateKey
    ("get", "/integration/patients/NOBODY/surfaces", None),            # PatientNotFoundInDatabase
    ("get", "/integration/patients/" + "9" * 65 + "/surfaces", None),  # invented: over 64
    ("get", "/integration/surfaces/no-such-surface", None),            # SurfaceNotFound
    ("post", "/integration/GetMap", {**SETUP, "PatientSurfaceId": "nope"}),
    ("post", "/integration/GetMap", {**SETUP, "TreatmentRoomId": "nope"}),
    ("post", "/integration/GetMap", {**SETUP, "PatientPosition": "SIDEWAYS"}),  # 400
    ("post", "/integration/GetBeamDeliveryStatus", {**BEAMS, "PatientSurfaceId": "nope"}),
    ("post", "/integration/GetBeamDeliveryStatus", {**SETUP, "BeamList": "not a list"}),  # 400
]


def _call(client, method, path, payload):
    return client.post(path, json=payload) if method == "post" else client.get(path)


class TestRepliesHaveExactlyTheRecordedKeys:
    @pytest.mark.parametrize("method,path,payload,model", SUCCESSES,
                             ids=[f"{m} {p}" for m, p, _b, _m in SUCCESSES])
    def test_a_success(self, fast_client, method, path, payload, model):
        reply = json.loads(_call(fast_client, method, path, payload).text)
        model.model_validate(reply)
        assert list(reply) == ["data", "errorData", "success", "errorCode", "errorCodeType"]

    @pytest.mark.parametrize("method,path,payload", FAILURES,
                             ids=[f"{m} {p[:40]}" for m, p, _b in FAILURES])
    def test_a_failure(self, fast_client, method, path, payload):
        reply = json.loads(_call(fast_client, method, path, payload).text)
        replies.ErrorReply.model_validate(reply)
        assert list(reply) == ["errorData", "success", "errorCode", "errorCodeType"]

    @pytest.mark.parametrize("settings", [
        {"token": "a-different-token"},            # the client's token is now wrong
        {"unlicensed": ("map", "beams", "surfaces")},
        {"scenario": "server-error"},
        {"scenario": "throttle"},
    ], ids=["bad-token", "unlicensed", "server-error", "throttle"])
    def test_a_failure_from_auth_licensing_or_a_scenario(self, client_factory, settings):
        with client_factory(map_seconds=0.0, **settings) as client:
            for method, path, payload in [("get", f"/integration/surfaces/{SURFACE}", None),
                                          ("post", "/integration/GetMap", SETUP)]:
                reply = json.loads(_call(client, method, path, payload).text)
                replies.ErrorReply.model_validate(reply)

    def test_a_user_agent_failure(self, client_factory):
        with client_factory(user_agent="curl/8.7.1") as client:
            replies.ErrorReply.model_validate(json.loads(client.get("/integration/ping").text))

    def test_the_models_refuse_a_key_an_appliance_does_not_send(self):
        with pytest.raises(ValidationError):
            replies.ErrorReply.model_validate({"data": None, "errorData": None, "success": False,
                                               "errorCode": "X", "errorCodeType": "Y"})
        with pytest.raises(ValidationError):
            replies.PingReply.model_validate({"data": "2.0.0", "errorData": None, "success": True,
                                              "errorCode": None, "errorCodeType": "NoErrorCodes",
                                              "extra": 1})


class TestNoEmulatorHeadersByDefault:
    @pytest.mark.parametrize("method,path,payload", [
        ("get", "/integration/ping", None),
        ("post", "/integration/GetMap", SETUP),
        ("post", "/integration/GetBeamDeliveryStatus", BEAMS),
    ])
    def test_none_is_sent(self, fast_client, method, path, payload):
        response = _call(fast_client, method, path, payload)
        assert not [name for name in response.headers if name.lower().startswith("x-emulator")]

    def test_the_beam_source_header_is_sent_on_request(self, client_factory):
        with client_factory(source_headers=True) as client:
            response = client.post("/integration/GetBeamDeliveryStatus", json=BEAMS)
        assert response.headers["X-Emulator-Beam-Source"] == "synthetic"


class TestTheDocsPage:
    @pytest.fixture
    def schema(self, fast_client):
        return json.loads(fast_client.get("/openapi.json").text)  # audit: allow writable-locations - a URL path, not a file

    def test_every_route_takes_the_bearer_token(self, schema):
        assert schema["components"]["securitySchemes"]["HTTPBearer"]["scheme"] == "bearer"
        for path, operations in schema["paths"].items():
            for operation in operations.values():
                assert operation["security"] == [{"HTTPBearer": []}], path

    def test_no_reply_fastapi_would_send_but_the_emulator_never_does(self, schema):
        # A malformed body is the MapRT envelope at 400, never FastAPI's 422 {"detail": …}.
        for path, operations in schema["paths"].items():
            for operation in operations.values():
                assert "422" not in operation["responses"], path
        assert "HTTPValidationError" not in schema["components"]["schemas"]

    def test_the_error_envelope_has_no_data_key(self, schema):
        error = schema["components"]["schemas"]["ErrorReply"]
        assert list(error["properties"]) == ["errorData", "success", "errorCode", "errorCodeType"]
        assert error["additionalProperties"] is False

    def test_getmap_documents_text_and_the_error_envelope(self, schema):
        content = schema["paths"]["/integration/GetMap"]["post"]["responses"]["200"]["content"]
        assert content["text/plain"]["example"].endswith("DONE\r\n")
        assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorReply"}

    def test_the_bearer_scheme_does_not_check_anything_itself(self, client_factory):
        # require_token answers a bad credential with the envelope at 200; the scheme added for
        # /docs must not turn that into FastAPI's 403.
        with client_factory(token=None) as client:
            response = client.get("/integration/rooms", headers={"Authorization": "Basic abc"})
        assert response.status_code == 200
        assert json.loads(response.text)["errorCode"] == "InvalidClientToken"
