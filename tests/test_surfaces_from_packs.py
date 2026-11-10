"""The patient and surface routes with packs loaded (decision 2, rules D and H).

A served body must be the recorded one byte for byte, so replies are compared as compact JSON.
"""

import json
import logging

import pytest
from pack_builder import PATIENT, SESSION, SURFACE, clean_files, edit_record, write_pack

LIST_STEM = f"integration_patients_{PATIENT}_surfaces_20261020-090003.000000"
FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"
SURFACE_NOT_FOUND = {"errorData": None, "success": False, "errorCode": "SurfaceNotFound",
                     "errorCodeType": "PatientErrorCodes"}


def recorded(files, stem):
    return json.loads(files[f"sessions/{SESSION}/{stem}_response.json"])["body"]


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def unfetched_files():
    files = clean_files()
    del files[f"sessions/{SESSION}/{FETCH_STEM}_request.json"], files[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
    return files


def packs(tmp_path, *file_sets):
    folder = tmp_path / "packs"
    folder.mkdir(exist_ok=True)
    for n, files in enumerate(file_sets, 1):
        write_pack(folder / f"pack-{n}.zip", files, name=f"pack-{n}")
    return folder


class TestThePatientsList:
    def test_is_the_recorded_reply_byte_for_byte(self, client_factory, tmp_path):
        files = clean_files()
        with client_factory(packs_dir=packs(tmp_path, files)) as client:
            reply = client.get(f"/integration/patients/{PATIENT}/surfaces")
        assert reply.content == compact(recorded(files, LIST_STEM))

    def test_is_the_union_across_packs(self, client_factory, tmp_path):
        second = clean_files()
        body = recorded(second, LIST_STEM)
        body["data"].append({"id": OTHER, "timeStamp": "2026-10-21T08:00:00.1", "timeZone": "UTC"})
        edit_record(second, f"{LIST_STEM}_response.json", body=body)
        with client_factory(packs_dir=packs(tmp_path, clean_files(), second)) as client:
            data = client.get(f"/integration/patients/{PATIENT}/surfaces").json()["data"]
        assert [surface["id"] for surface in data] == [SURFACE, OTHER]

    def test_keeps_a_surface_that_was_never_fetched(self, client_factory, tmp_path):
        files = unfetched_files()
        with client_factory(packs_dir=packs(tmp_path, files)) as client:
            reply = client.get(f"/integration/patients/{PATIENT}/surfaces")
        assert reply.content == compact(recorded(files, LIST_STEM))

    def test_an_unknown_patient_is_the_emulators_own_error(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs(tmp_path, clean_files())) as client:
            reply = client.get("/integration/patients/ANON001/surfaces").json()   # in the catalog, not the packs
        assert reply["errorCode"] == "PatientNotFoundInDatabase"

    def test_a_recorded_error_is_served_as_recorded(self, client_factory, tmp_path):
        files = clean_files()
        error = {"errorData": None, "success": False, "errorCode": "SomethingRecorded",
                 "errorCodeType": "PatientErrorCodes"}
        edit_record(files, f"{LIST_STEM}_response.json", body=error)
        with client_factory(packs_dir=packs(tmp_path, files)) as client:
            reply = client.get(f"/integration/patients/{PATIENT}/surfaces")
        assert reply.content == compact(error)

    def test_a_long_patient_id_is_still_refused(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs(tmp_path, clean_files())) as client:
            assert client.get(f"/integration/patients/{'x' * 65}/surfaces").status_code == 400


class TestASurface:
    def test_a_fetched_one_is_the_recorded_reply_byte_for_byte(self, client_factory, tmp_path):
        files = clean_files()
        with client_factory(packs_dir=packs(tmp_path, files)) as client:
            reply = client.get(f"/integration/surfaces/{SURFACE}")
        assert reply.content == compact(recorded(files, FETCH_STEM))

    def test_one_fetched_by_another_pack_is_served(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs(tmp_path, unfetched_files(), clean_files())) as client:
            assert client.get(f"/integration/surfaces/{SURFACE}").json()["success"] is True

    def test_one_never_fetched_answers_surface_not_found_and_is_logged(self, client_factory, tmp_path, caplog):
        with client_factory(packs_dir=packs(tmp_path, unfetched_files())) as client, \
                caplog.at_level(logging.WARNING, logger="maprt_emulator"):
            reply = client.get(f"/integration/surfaces/{SURFACE}")
        assert reply.status_code == 200 and reply.content == compact(SURFACE_NOT_FOUND)
        assert "x-emulator-surface-source" not in reply.headers
        assert any(SURFACE in record.getMessage() and "listed but never fetched" in record.getMessage()
                   for record in caplog.records)

    def test_source_headers_name_it(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs(tmp_path, unfetched_files()), source_headers=True) as client:
            reply = client.get(f"/integration/surfaces/{SURFACE}")
        assert reply.headers["x-emulator-surface-source"] == "unfetched"

    def test_a_recorded_error_is_served_as_recorded(self, client_factory, tmp_path):
        files = clean_files()
        from pack_builder import as_json, record_pair
        error = {"errorData": None, "success": False, "errorCode": "SomethingRecorded",
                 "errorCodeType": "PatientErrorCodes"}
        files.update({f"sessions/{SESSION}/{name}": as_json(record) for name, record in record_pair(
            f"integration_surfaces_{OTHER}_20261020-090009.000000", "GET", f"/integration/surfaces/{OTHER}", None,
            {"body": error}).items()})
        with client_factory(packs_dir=packs(tmp_path, files)) as client:
            assert client.get(f"/integration/surfaces/{OTHER}").content == compact(error)

    def test_an_unknown_one_is_the_emulators_own_error(self, client_factory, tmp_path):
        with client_factory(packs_dir=packs(tmp_path, clean_files())) as client:
            assert client.get(f"/integration/surfaces/{OTHER}").content == compact(SURFACE_NOT_FOUND)

    @pytest.mark.parametrize("unlicensed", [("surfaces",)])
    def test_unlicensed_still_applies(self, client_factory, tmp_path, unlicensed):
        with client_factory(packs_dir=packs(tmp_path, clean_files()), unlicensed=unlicensed) as client:
            assert client.get(f"/integration/surfaces/{SURFACE}").json()["errorCode"] == "FeatureNotLicensed"
