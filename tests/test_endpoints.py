"""The five JSON-envelope endpoints, against the shipped catalog.

These describe the API's behaviour as a notebook will meet it, including the three answers that
are easy to get wrong: a room name matching nothing, a room name matching several, and a known
patient with no surfaces.
"""

import base64
import json

import pytest

from maprt_emulator.settings import API_VERSION, SERVER_VERSION
from maprt_emulator.store import EMPTY_GUID

SURFACE_1 = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"   # ANON001, older
SURFACE_2 = "a6f3fbee-6714-4f92-80c1-f5aadda4cbeb"   # ANON001, newer


class TestPing:
    def test_reports_the_api_version_as_a_string(self, client, envelope_of):
        body = envelope_of(client.get("/integration/ping"))
        assert body["data"] == API_VERSION
        assert isinstance(body["data"], str)

    def test_every_response_carries_the_saturn_server_header(self, client):
        # Spec §5.1. It is how a client can tell what it is talking to, and the emulator has to
        # replace uvicorn's own header to be honest about mimicking an appliance.
        response = client.get("/integration/ping")
        assert response.headers["server"] == SERVER_VERSION


class TestAllRooms:
    def test_lists_every_room_with_all_four_fields(self, client, envelope_of):
        rooms = envelope_of(client.get("/integration/rooms"))["data"]
        assert len(rooms) == 4
        for room in rooms:
            assert set(room) == {"id", "machineName", "name", "coordinateSystem"}

    def test_room_ids_are_unique_even_though_machine_names_are_not(self, client, envelope_of):
        # The reason production code keys on the GUID. Both halves of this assertion matter.
        rooms = envelope_of(client.get("/integration/rooms"))["data"]
        ids = [room["id"] for room in rooms]
        machine_names = [room["machineName"] for room in rooms]
        assert len(set(ids)) == len(ids)
        assert len(set(machine_names)) < len(machine_names)

    def test_carries_a_non_iec_coordinate_system_too(self, client, envelope_of):
        # A room reports the couch coordinate system its Vision RT interface displays in, which
        # is not always IEC_61217. A notebook needs a Native room to show that.
        rooms = envelope_of(client.get("/integration/rooms"))["data"]
        assert {room["coordinateSystem"] for room in rooms} == {"IEC_61217", "Native"}


class TestSingleRoom:
    def test_a_unique_machine_name_resolves_to_its_guid(self, client, envelope_of):
        body = envelope_of(client.get("/integration/rooms/TrueBeam2"))
        assert body["success"] is True
        assert body["data"] == "4d1f6e02-7c3b-4a19-9f55-2b8e0c71a3d7"

    def test_no_match_is_a_success_carrying_the_all_zeros_guid(self, client, envelope_of):
        # Not an error. A client that only checks `success` will carry this GUID into a GetMap
        # call and be baffled by the result.
        body = envelope_of(client.get("/integration/rooms/NoSuchMachine"))
        assert body["success"] is True
        assert body["data"] == EMPTY_GUID

    def test_a_shared_machine_name_is_a_duplicate_key_error_at_http_200(self, client, envelope_of):
        response = client.get("/integration/rooms/TrueBeam")
        assert response.status_code == 200
        body = envelope_of(response)
        assert body["success"] is False
        assert body["errorCode"] == "DuplicateKey"
        assert body["errorCodeType"] == "TreatmentRoomAdministrationErrorCodes"

    def test_an_over_long_machine_name_simply_matches_nothing(self, client, envelope_of):
        # The spec caps machine names at 16 characters, but the appliance does not enforce it: a
        # longer one is answered like any unknown name (recorded 2026-10-23).
        response = client.get("/integration/rooms/" + "x" * 17)
        assert response.status_code == 200
        body = envelope_of(response)
        assert body["success"] is True
        assert body["data"] == EMPTY_GUID


class TestPatientSurfaces:
    def test_lists_a_patients_surfaces_with_timestamp_and_timezone(self, client, envelope_of):
        surfaces = envelope_of(client.get("/integration/patients/ANON001/surfaces"))["data"]
        assert len(surfaces) == 2
        for surface in surfaces:
            assert set(surface) == {"id", "timeStamp", "timeZone"}

    def test_newest_first(self, client, envelope_of):
        # "Use the most recent surface" is the common requirement, so the order is worth pinning.
        surfaces = envelope_of(client.get("/integration/patients/ANON001/surfaces"))["data"]
        assert [s["id"] for s in surfaces] == [SURFACE_2, SURFACE_1]

    def test_a_known_patient_with_no_surfaces_is_an_empty_list_and_a_success(self, client,
                                                                            envelope_of):
        body = envelope_of(client.get("/integration/patients/ANON003/surfaces"))
        assert body["success"] is True
        assert body["data"] == []

    def test_an_unknown_patient_is_an_error_not_an_empty_list(self, client, envelope_of):
        # The distinction the previous test sets up. Same shape of question, different answer.
        body = envelope_of(client.get("/integration/patients/NOBODY/surfaces"))
        assert body["success"] is False
        # As recorded from an appliance (2026-10-23).
        assert (body["errorCode"], body["errorCodeType"], body["errorData"]) \
            == ("PatientNotFoundInDatabase", "PatientErrorCodes", None)

    def test_an_over_long_patient_id_is_a_malformed_request(self, client):
        # Spec §5.2.2: DICOM patient ids are at most 64 characters.
        assert client.get("/integration/patients/" + "x" * 65 + "/surfaces").status_code == 400


class TestSurface:
    def test_returns_a_base64_data_uri(self, client, envelope_of):
        data = envelope_of(client.get(f"/integration/surfaces/{SURFACE_1}"))["data"]
        assert data.startswith("data:application/octet-stream;base64,")

    def test_the_payload_decodes_to_the_obj_bytes(self, client, envelope_of):
        data = envelope_of(client.get(f"/integration/surfaces/{SURFACE_1}"))["data"]
        raw = base64.b64decode(data.split(",")[-1])
        assert raw.startswith(b"#")            # the comment banner a MapRT export carries
        assert b"\nv " in raw and b"\nf " in raw

    def test_the_payload_parses_with_the_workshop_obj_reader(self, client, envelope_of):
        # End to end: what the emulator serves is what the client library can read.
        from maprt_emulator.obj import parse_obj
        data = envelope_of(client.get(f"/integration/surfaces/{SURFACE_1}"))["data"]
        vertices, faces, _normals = parse_obj(base64.b64decode(data.split(",")[-1]))
        assert len(vertices) > 1000
        assert len(faces) > 1000

    def test_an_unknown_surface_is_an_error(self, client, envelope_of):
        body = envelope_of(client.get("/integration/surfaces/00000000-dead-beef-0000-000000000000"))
        assert body["success"] is False
        assert body["errorCode"] == "SurfaceNotFound"

    def test_unlicensed_surfaces_report_feature_not_licensed_at_http_200(self, client_factory,
                                                                        envelope_of):
        # Surface data is separately licensed (§2.3.3), so this is a normal case to handle.
        with client_factory(unlicensed=("surfaces",)) as client:
            response = client.get(f"/integration/surfaces/{SURFACE_1}")
        assert response.status_code == 200
        body = envelope_of(response)
        assert body["errorCode"] == "FeatureNotLicensed"
        assert body["errorCodeType"] == "LicenseErrorCodes"

    def test_an_unlicensed_server_still_lists_surfaces(self, client_factory, envelope_of):
        # The metadata listing is not the licensed part; fetching the mesh is.
        with client_factory(unlicensed=("surfaces",)) as client:
            body = envelope_of(client.get("/integration/patients/ANON001/surfaces"))
        assert body["success"] is True


class TestEveryEndpointAnswersWithTheEnvelope:
    @pytest.mark.parametrize("path", [
        "/integration/ping",
        "/integration/rooms",
        "/integration/rooms/TrueBeam2",
        "/integration/rooms/TrueBeam",            # the DuplicateKey error path
        "/integration/rooms/NoSuchMachine",
        "/integration/patients/ANON001/surfaces",
        "/integration/patients/ANON003/surfaces",  # the empty-list path
        "/integration/patients/NOBODY/surfaces",   # the error path
        f"/integration/surfaces/{SURFACE_1}",
    ])
    def test_the_recorded_keys_are_always_present(self, client, path):
        # The appliance's two envelopes, key for key and in its order: five keys on success, four
        # (no data) on failure. FastAPI's own {"detail": …} error body would break that, so every
        # route has to raise MapRTFailure rather than HTTPException.
        body = json.loads(client.get(path).text)
        if body.get("success") is True:
            assert list(body) == ["data", "errorData", "success", "errorCode", "errorCodeType"], path
        else:
            assert list(body) == ["errorData", "success", "errorCode", "errorCodeType"], path
