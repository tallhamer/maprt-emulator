"""GetBeamDeliveryStatus with packs loaded: the serving order in ``routes/beams.py``.

Recorded for this request → a recorded error → the emulator's own errors (rule A for a surface
never fetched) → derived from the recorded high-resolution map of the same parameters → the model.
"""

import json
import logging

import pytest
from pack_builder import ROOM, SESSION, SURFACE, as_json, clean_files, record_pair, write_pack

FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"
COMMON = {"CouchBuffer": 20.0, "PatientBuffer": 20.0, "PatientPosition": "HFS", "PatientSurfaceId": SURFACE,
          "TreatmentRoomId": ROOM, "Isocenter": {"x": 0.0, "y": 98.6, "z": -1.71}}
RECORDED_BEAMS = [{"beamNumber": "1", "controlPoints": [{"gantryAngle": 0, "couchAngle": 0}]}]
#: A high-resolution map of the same parameters, holding only the cells the tests ask about.
HIGH_MAP = b"181,360\r\n0,0,1\r\n0,90,0\r\n10,45,1\r\n270,180,1\r\nDONE\r\n"


def beam(number, *points):
    return {"beamNumber": number, "controlPoints": [{"couchAngle": c, "gantryAngle": g} for c, g in points]}


def with_call(files, stem, url, body, reply):
    files.update({f"sessions/{SESSION}/{name}": as_json(record) for name, record in
                  record_pair(stem, "POST", url, body, reply).items()})
    return files


def with_high_map(files=None, **changes):
    files = clean_files() if files is None else files
    stem = "integration_GetMap_20261020-090009.000000"
    with_call(files, stem, "/integration/GetMap", {**COMMON, "HighResolution": True, **changes},
              {"body_file": f"{stem}_response.txt"})
    files[f"sessions/{SESSION}/{stem}_response.txt"] = HIGH_MAP
    return files


def unfetched_files():
    files = clean_files()
    del files[f"sessions/{SESSION}/{FETCH_STEM}_request.json"], files[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
    return files


@pytest.fixture
def post(client_factory, tmp_path):
    def _post(beams, files=None, **changes):
        folder = tmp_path / "packs"
        folder.mkdir(exist_ok=True)
        write_pack(folder / "a.zip", clean_files() if files is None else files, name="a")
        settings = {key: changes.pop(key) for key in ("unlicensed",) if key in changes}
        with client_factory(packs_dir=folder, source_headers=True, **settings) as client:
            return client.post("/integration/GetBeamDeliveryStatus", json={**COMMON, **changes, "BeamList": beams})
    return _post


def statuses(reply):
    return [(s["beamNumber"], s["isSafe"]) for s in reply.json()["data"]["statuses"]]


class TestRecorded:
    def test_the_verdict_for_this_request_as_recorded(self, post):
        reply = post(RECORDED_BEAMS)
        assert reply.headers["x-emulator-beam-source"] == "recorded"
        assert reply.content == json.dumps({"data": {"statuses": [{"beamNumber": 1, "isSafe": True}]},
                                            "errorData": None, "success": True, "errorCode": None,
                                            "errorCodeType": "NoErrorCodes"}, separators=(",", ":")).encode()

    def test_matches_a_beam_number_int_and_extra_fields(self, post):
        beams = [{"beamNumber": 1, "controlPoints": [{"gantryAngle": 0.0, "couchAngle": 0,
                                                      "coordinateSystem": "IEC_61217"}]}]
        assert post(beams).headers["x-emulator-beam-source"] == "recorded"

    def test_a_recorded_error_for_this_request(self, post):
        error = {"errorData": None, "success": False, "errorCode": "SomethingRecorded", "errorCodeType": "PlanErrorCodes"}
        files = with_call(clean_files(), "integration_GetBeamDeliveryStatus_20261020-090009.000000",
                          "/integration/GetBeamDeliveryStatus", {**COMMON, "BeamList": [beam(7, (5, 5))]},
                          {"body": error})
        assert post([beam(7, (5, 5))], files).json() == error

    def test_is_served_on_a_surface_never_fetched(self, post):
        assert post(RECORDED_BEAMS, unfetched_files()).headers["x-emulator-beam-source"] == "recorded"


class TestOwnErrors:
    def test_a_surface_never_fetched_is_surface_not_found(self, post, caplog):
        # Rule A: no mesh to judge it by, and no recorded verdict.
        with caplog.at_level(logging.WARNING, logger="maprt_emulator"):
            reply = post([beam(1, (5, 5))], with_high_map(unfetched_files())).json()
        assert reply["errorCode"] == "SurfaceNotFound"
        assert any("listed but never fetched" in record.getMessage() for record in caplog.records)

    def test_an_unknown_surface(self, post):
        assert post([beam(1, (5, 5))], PatientSurfaceId=OTHER).json()["errorCode"] == "SurfaceNotFound"

    def test_a_room_not_in_the_union(self, post):
        assert post([beam(1, (5, 5))], TreatmentRoomId=OTHER).json()["errorCode"] == "TreatmentRoomNotFound"

    def test_unlicensed_still_applies(self, post):
        assert post(RECORDED_BEAMS, unlicensed=("beams",)).json()["errorCode"] == "FeatureNotLicensed"


class TestDerivedFromARecordedHighResMap:
    def test_each_beam_is_judged_at_its_control_points(self, post):
        reply = post([beam(1, (0, 0)), beam(2, (0, 0), (0, 90)), beam(3, (9.6, 44.6), (270, 180))], with_high_map())
        assert reply.headers["x-emulator-beam-source"] == "derived"
        assert statuses(reply) == [(1, True), (2, False), (3, True)]

    def test_angles_wrap_to_the_maps_degrees(self, post):
        assert statuses(post([beam(1, (360, -0.4))], with_high_map())) == [(1, True)]

    def test_a_beam_with_no_control_points_is_not_safe(self, post):
        assert statuses(post([beam(1, (0, 0)), beam(2)], with_high_map())) == [(1, True), (2, False)]

    def test_a_control_point_off_the_map_falls_back_to_the_model(self, post):
        assert post([beam(1, (0, 0)), beam(2, (45, 45))], with_high_map()).headers["x-emulator-beam-source"] == \
            "synthetic"

    def test_only_a_map_of_the_same_parameters(self, post):
        assert post([beam(5, (0, 0))], with_high_map(CouchBuffer=30.0)).headers["x-emulator-beam-source"] == \
            "synthetic"

    def test_only_a_high_resolution_map(self, post):
        files = with_high_map()
        stem = "integration_GetMap_20261020-090009.000000"
        record = json.loads(files[f"sessions/{SESSION}/{stem}_request.json"])
        record["body"]["HighResolution"] = False
        files[f"sessions/{SESSION}/{stem}_request.json"] = as_json(record)
        assert post([beam(5, (0, 0))], files).headers["x-emulator-beam-source"] == "synthetic"


def test_with_nothing_recorded_the_model_judges_the_recorded_mesh(post):
    reply = post([beam(1, (0, 0)), beam(2, (90, 270))])
    assert reply.headers["x-emulator-beam-source"] == "synthetic"
    assert [number for number, _safe in statuses(reply)] == [1, 2]
