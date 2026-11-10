"""GetMap with packs loaded: the serving order in ``routes/getmap.py``.

Recorded for this request → a recorded error → the emulator's own errors → a recorded map of the
requested resolution chosen for this request (not its data) → the model's map from the recorded mesh.
"""

import json
import logging

import pytest
from pack_builder import MAP, ROOM, SESSION, SURFACE, as_json, clean_files, record_pair, write_pack

GETMAP_STEM = "integration_GetMap_20261020-090005.000000"
FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"
REQUEST = {"CouchBuffer": 20, "PatientBuffer": 20.0, "HighResolution": False, "PatientPosition": "HFS",
           "PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "Isocenter": {"x": 0.0, "y": 98.6, "z": -1.71}}
HIGH_MAP = b"181,361\r\n0,0,0\r\nDONE\r\n"


def add_map(files, seconds, body, text):
    stem = f"integration_GetMap_20261020-0900{seconds}.000000"
    files.update({f"sessions/{SESSION}/{name}": as_json(record) for name, record in record_pair(
        stem, "POST", "/integration/GetMap", body, {"body_file": f"{stem}_response.txt"}).items()})
    files[f"sessions/{SESSION}/{stem}_response.txt"] = text


def packs_dir(tmp_path, files=None):
    folder = tmp_path / "packs"
    folder.mkdir(exist_ok=True)
    write_pack(folder / "a.zip", clean_files() if files is None else files, name="a")
    return folder


@pytest.fixture
def post(client_factory, tmp_path):
    def _post(body, files=None, **settings):
        with client_factory(packs_dir=packs_dir(tmp_path, files), map_seconds=0.0, source_headers=True,
                            **settings) as client:
            return client.post("/integration/GetMap", json=body)
    return _post


class TestRecordedForThisRequest:
    def test_is_served_byte_for_byte(self, post):
        reply = post(REQUEST)
        assert reply.content == MAP and reply.headers["x-emulator-map-source"] == "recorded"
        assert "content-type" not in reply.headers

    def test_matches_on_the_fields_the_route_knows(self, post):
        isocenter = {"x": -0.0, "y": 98.6, "z": -1.71, "CoordinateSystem": "IEC_61217"}
        reply = post({**REQUEST, "CouchBuffer": 20.0, "PatientPosition": 1, "Isocenter": isocenter})
        assert reply.content == MAP

    def test_is_served_on_a_surface_never_fetched(self, post):
        # Rule A: a recorded reply for that exact request is served as usual.
        files = clean_files()
        del files[f"sessions/{SESSION}/{FETCH_STEM}_request.json"], files[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
        assert post(REQUEST, files).content == MAP

    def test_only_this_with_map_source_recorded(self, post):
        assert post(REQUEST, map_source="recorded").content == MAP
        reply = post({**REQUEST, "CouchBuffer": 30.0}, map_source="recorded").json()
        assert reply["errorCode"] == "NoRecordedMap"

    def test_is_skipped_with_map_source_synthetic(self, post):
        reply = post(REQUEST, map_source="synthetic")
        assert reply.headers["x-emulator-map-source"] == "synthetic" and reply.content != MAP


class TestErrors:
    def test_a_recorded_error_for_this_request(self, post):
        files = clean_files()
        error = {"errorData": None, "success": False, "errorCode": "TreatmentRoomNotFound",
                 "errorCodeType": "PlanErrorCodes"}
        files.update({f"sessions/{SESSION}/{name}": as_json(record) for name, record in record_pair(
            "integration_GetMap_20261020-090009.000000", "POST", "/integration/GetMap",
            {**REQUEST, "TreatmentRoomId": OTHER}, {"body": error}).items()})
        assert post({**REQUEST, "TreatmentRoomId": OTHER}, files).content == json.dumps(
            error, separators=(",", ":")).encode()

    def test_an_unfetched_surface_with_no_recorded_map_is_surface_not_found(self, post, caplog):
        files = clean_files()
        del files[f"sessions/{SESSION}/{FETCH_STEM}_request.json"], files[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
        with caplog.at_level(logging.WARNING, logger="maprt_emulator"):
            reply = post({**REQUEST, "CouchBuffer": 30.0}, files).json()
        assert reply["errorCode"] == "SurfaceNotFound"
        assert any("listed but never fetched" in record.getMessage() for record in caplog.records)

    def test_an_unknown_surface(self, post):
        assert post({**REQUEST, "PatientSurfaceId": OTHER}).json()["errorCode"] == "SurfaceNotFound"

    def test_a_room_not_in_the_union(self, post):
        reply = post({**REQUEST, "TreatmentRoomId": OTHER}).json()
        assert (reply["errorCode"], reply["errorCodeType"]) == ("TreatmentRoomNotFound", "PlanErrorCodes")

    def test_unlicensed_still_applies(self, post):
        assert post(REQUEST, unlicensed=("map",)).json()["errorCode"] == "FeatureNotLicensed"


class TestNotRecordedForThisRequest:
    def test_gets_a_recorded_map_of_its_resolution_labelled_and_logged(self, post, caplog):
        with caplog.at_level(logging.WARNING, logger="maprt_emulator"):
            reply = post({**REQUEST, "CouchBuffer": 30.0})
        assert reply.content == MAP and reply.headers["x-emulator-map-source"] == "random-recorded"
        assert any("NOT this request's data" in record.getMessage() for record in caplog.records)

    def test_only_of_the_requested_resolution(self, post):
        files = clean_files()
        add_map(files, "09", {**REQUEST, "HighResolution": True}, HIGH_MAP)
        reply = post({**REQUEST, "HighResolution": True, "CouchBuffer": 30.0}, files)
        assert reply.content == HIGH_MAP and reply.headers["x-emulator-map-source"] == "random-recorded"

    def test_the_same_request_always_gets_the_same_map(self, post):
        files = clean_files()
        for n in range(10, 20):
            add_map(files, str(n), {**REQUEST, "CouchBuffer": float(n)}, f"37,36\r\n{n},0,1\r\nDONE\r\n".encode())
        body = {**REQUEST, "CouchBuffer": 99.0}
        assert len({post(body, files).content for _ in range(3)}) == 1
        assert len({post({**REQUEST, "CouchBuffer": float(b)}, files).content for b in range(40, 60)}) > 1

    def test_with_none_of_its_resolution_the_model_computes_one_from_the_recorded_mesh(self, post):
        reply = post({**REQUEST, "HighResolution": True})
        assert reply.headers["x-emulator-map-source"] == "synthetic"
        lines = reply.text.splitlines()
        couch, gantry = map(int, lines[0].split(","))
        assert len(lines) == couch * gantry + 2 and lines[-1] == "DONE"

    def test_no_source_header_by_default(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs_dir(tmp_path), map_seconds=0.0) as client:
            assert "x-emulator-map-source" not in client.post("/integration/GetMap", json=REQUEST).headers
