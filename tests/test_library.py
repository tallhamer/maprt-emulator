"""The pack library: its indexes, the rules for packs that disagree, and which packs load.

Each pack here is ``pack_builder``'s clean one (one patient, a call per endpoint), edited to make
the case; they share the patient, room and surface, so two of them overlap the way two sessions of
one phantom would.
"""

import json

import pytest
from pack_builder import PATIENT, ROOM, SESSION, SURFACE, as_json, clean_files, edit_record, ok, record_pair, write_pack

from maprt_emulator.library import (
    BEAMS,
    GETMAP,
    NO_PACKS_HINT,
    PATIENT_SURFACES,
    PackConflictError,
    PackLibrary,
    PacksRefused,
    recorded_key,
    request_key,
    route_of,
)
from maprt_emulator.library import SURFACE as SURFACE_ROUTE
from maprt_emulator.packs import load_pack
from maprt_emulator.schemas import GetMapRequest

PREFIX = f"sessions/{SESSION}/"
ROOMS_STEM = "integration_rooms_20261020-090002.000000"
LIST_STEM = f"integration_patients_{PATIENT}_surfaces_20261020-090003.000000"
FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"
GETMAP_STEM = "integration_GetMap_20261020-090005.000000"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"
MAP_BODY = {"CouchBuffer": 20, "PatientBuffer": 20.0, "HighResolution": False, "PatientPosition": "HFS",
            "PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "Isocenter": {"x": 0.0, "y": 98.6, "z": -1.71}}


def add_call(files, stem, method, url, body, reply, **kwargs):
    files.update({PREFIX + name: as_json(record) for name, record in
                  record_pair(stem, method, url, body, reply, **kwargs).items()})


def reply_of(files, stem):
    return json.loads(files[f"{PREFIX}{stem}_response.json"])["body"]


def make(folder, name, files=None, **manifest):
    folder.mkdir(exist_ok=True)
    return load_pack(write_pack(folder / f"{name}.zip", clean_files() if files is None else files, name=name,
                                **manifest))


def library(tmp_path, *file_sets, **manifest):
    return PackLibrary.build([make(tmp_path / "packs", f"pack-{n}", files, **manifest)
                              for n, files in enumerate(file_sets, 1)])


class TestOneCleanPack:
    def test_indexes_what_was_recorded(self, tmp_path):
        lib = library(tmp_path, clean_files())
        assert list(lib.rooms) == [ROOM]
        assert [s["id"] for s in lib.patients[PATIENT]] == [SURFACE]
        assert list(lib.surfaces) == [SURFACE]
        assert lib.unfetched == {}
        assert [e.path for e in lib.maps.values()] == ["/integration/GetMap"]
        assert [e.path for e in lib.verdicts.values()] == ["/integration/GetBeamDeliveryStatus"]
        assert lib.errors == {}
        assert lib.appliance == {"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"}
        assert lib.notices == []

    def test_summary(self, tmp_path):
        assert library(tmp_path, clean_files()).summary() == \
            "1 pack, 1 patient, 1 surface, 1 room, 1 map, 1 beam check"

    def test_no_packs(self):
        lib = PackLibrary.build([])
        assert lib.appliance is None and lib.summary().startswith("0 packs")


class TestTheRequestKey:
    """Rule F: only the fields the route's model knows, normalised as the route parses them."""

    def key(self, **changes):
        return recorded_key(GETMAP, {**MAP_BODY, **changes})

    def test_an_int_matches_a_float(self):
        assert self.key(CouchBuffer=20) == self.key(CouchBuffer=20.0)

    def test_key_order_doesnt_matter(self):
        assert recorded_key(GETMAP, dict(reversed(list(MAP_BODY.items())))) == self.key()

    def test_a_field_the_appliance_ignores_doesnt_count(self):
        isocenter = {**MAP_BODY["Isocenter"], "CoordinateSystem": "IEC_61217"}
        assert self.key(Isocenter=isocenter, Extra="ignored") == self.key()

    def test_a_patient_position_number_matches_its_name(self):
        assert self.key(PatientPosition=1) == self.key(PatientPosition="HFS")

    def test_negative_zero_is_zero(self):
        assert self.key(Isocenter={"x": -0.0, "y": 98.6, "z": -1.71}) == self.key()

    def test_high_resolution_counts(self):
        assert self.key(HighResolution=True) != self.key()

    def test_values_are_matched_exactly(self):
        assert self.key(Isocenter={"x": 0.0, "y": 98.60001, "z": -1.71}) != self.key()

    def test_the_route_builds_the_same_key(self):
        assert request_key(GetMapRequest.model_validate(MAP_BODY)) == self.key()

    def test_a_beam_number_string_matches_an_int(self):
        beams = {"PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "Isocenter": {"x": 0, "y": 0, "z": 0}}
        assert recorded_key(BEAMS, {**beams, "BeamList": [{"beamNumber": "1", "controlPoints": []}]}) == \
            recorded_key(BEAMS, {**beams, "BeamList": [{"beamNumber": 1, "controlPoints": []}]})

    def test_a_body_the_route_would_refuse_has_no_key(self):
        assert recorded_key(GETMAP, {"CouchBuffer": 20}) is None


def test_route_of():
    assert route_of("/integration/rooms/Truebeam ") == ("/integration/rooms/{machineName}", "Truebeam ")
    assert route_of(f"/integration/patients/{PATIENT}/surfaces") == (PATIENT_SURFACES, PATIENT)
    assert route_of(f"/integration/surfaces/{SURFACE}") == (SURFACE_ROUTE, SURFACE)
    assert route_of("/integration/GetMap") == (GETMAP, None)
    assert route_of("/elsewhere") == (None, None)


class TestRooms:
    def test_identical_rooms_in_two_packs_are_one(self, tmp_path):
        assert list(library(tmp_path, clean_files(), clean_files()).rooms) == [ROOM]

    def test_the_union_is_first_seen(self, tmp_path):
        second = clean_files()
        rooms = reply_of(second, ROOMS_STEM)
        rooms["data"].insert(0, {"name": "TB2", "id": OTHER, "machineName": "Truebeam", "coordinateSystem": "IEC_61217"})
        edit_record(second, f"{ROOMS_STEM}_response.json", body=rooms)
        assert list(library(tmp_path, clean_files(), second).rooms) == [ROOM, OTHER]

    def test_one_guid_with_different_fields_refuses(self, tmp_path):
        second = clean_files()
        rooms = reply_of(second, ROOMS_STEM)
        rooms["data"][0]["name"] = "TB1 "
        edit_record(second, f"{ROOMS_STEM}_response.json", body=rooms)
        with pytest.raises(PackConflictError) as error:
            library(tmp_path, clean_files(), second)
        message = str(error.value)
        assert ROOM in message and "pack-1" in message and "pack-2" in message
        assert message.endswith(NO_PACKS_HINT)

    def test_a_failed_rooms_call_adds_nothing(self, tmp_path):
        files = clean_files()
        edit_record(files, f"{ROOMS_STEM}_response.json",
                    body={"errorData": None, "success": False, "errorCode": "X", "errorCodeType": "Y"})
        lib = library(tmp_path, files)
        assert lib.rooms == {} and lib.errors == {}


class TestPatientsAndSurfaces:
    def test_surface_lists_union_by_id(self, tmp_path):
        second = clean_files()
        listed = reply_of(second, LIST_STEM)
        listed["data"].append({"id": OTHER, "timeStamp": "2026-10-21T08:00:00.1", "timeZone": "UTC"})
        edit_record(second, f"{LIST_STEM}_response.json", body=listed)
        lib = library(tmp_path, clean_files(), second)
        assert [s["id"] for s in lib.patients[PATIENT]] == [SURFACE, OTHER]

    def test_one_surface_with_two_timestamps_refuses(self, tmp_path):
        second = clean_files()
        listed = reply_of(second, LIST_STEM)
        listed["data"][0]["timeStamp"] = "2026-10-20T08:00:00.2"
        edit_record(second, f"{LIST_STEM}_response.json", body=listed)
        with pytest.raises(PackConflictError, match=f"Surface {SURFACE} of patient {PATIENT}"):
            library(tmp_path, clean_files(), second)

    def test_one_surface_with_different_bytes_refuses(self, tmp_path):
        second = clean_files()
        edit_record(second, f"{FETCH_STEM}_response.json", body=ok("data:application/octet-stream;base64,AAAA"))
        with pytest.raises(PackConflictError, match="different bytes"):
            library(tmp_path, clean_files(), second)


class TestUnfetchedSurfaces:
    """Decision 2: listed but never fetched, in any pack."""

    def unfetched_files(self):
        files = clean_files()
        del files[f"{PREFIX}{FETCH_STEM}_request.json"], files[f"{PREFIX}{FETCH_STEM}_response.json"]
        return files

    def test_is_kept_in_the_list_and_reported(self, tmp_path):
        lib = library(tmp_path, self.unfetched_files())
        assert [s["id"] for s in lib.patients[PATIENT]] == [SURFACE]
        assert SURFACE not in lib.surfaces
        assert lib.unfetched[SURFACE].packs == ("pack-1",)
        assert any("listed but never fetched; fetches answer SurfaceNotFound" in notice for notice in lib.notices)
        assert lib.summary().endswith("; 1 listed surface has no recorded mesh")

    def test_isnt_once_another_pack_fetched_it(self, tmp_path):
        lib = library(tmp_path, self.unfetched_files(), clean_files())
        assert lib.unfetched == {} and SURFACE in lib.surfaces


class TestMapsAndVerdicts:
    def test_the_first_recorded_reply_wins_and_repeats_are_counted(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_GetMap_20261020-090009.000000", "POST", "/integration/GetMap",
                 {**MAP_BODY, "CouchBuffer": 20.0}, {"body_file": "integration_GetMap_20261020-090009.000000_response.txt"})
        files[f"{PREFIX}integration_GetMap_20261020-090009.000000_response.txt"] = b"other map"
        lib = library(tmp_path, files)
        (served,) = lib.maps.values()
        assert served.stem == GETMAP_STEM
        assert any("1 recorded map or beam-check repeat" in notice for notice in lib.notices)

    def test_a_getmap_error_is_an_error_not_a_map(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_GetMap_20261020-090009.000000", "POST", "/integration/GetMap",
                 {**MAP_BODY, "TreatmentRoomId": OTHER},
                 {"body": {"errorData": None, "success": False, "errorCode": "TreatmentRoomNotFound",
                           "errorCodeType": "PlanErrorCodes"}})
        lib = library(tmp_path, files)
        assert len(lib.maps) == 1
        key = recorded_key(GETMAP, {**MAP_BODY, "TreatmentRoomId": OTHER})
        assert lib.errors[(GETMAP, key)].body["errorCode"] == "TreatmentRoomNotFound"


class TestErrors:
    def not_found(self):
        return {"errorData": None, "success": False, "errorCode": "PatientNotFoundInDatabase",
                "errorCodeType": "PatientErrorCodes"}

    def test_a_recorded_unknown_patient_is_indexed(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_patients_ANON-1_surfaces_20261020-090009.000000", "GET",
                 "/integration/patients/ANON-1/surfaces", None, {"body": self.not_found()})
        edit_record(files, f"{LIST_STEM}_response.json", body=self.not_found())
        lib = library(tmp_path, files)
        assert lib.errors[(PATIENT_SURFACES, PATIENT)].body["errorCode"] == "PatientNotFoundInDatabase"
        assert PATIENT not in lib.patients

    def test_a_success_beats_an_error_in_either_order(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_patients_ANON-1_surfaces_20261020-090000.000000", "GET",
                 "/integration/patients/ANON-1/surfaces", None, {"body": self.not_found()})
        lib = library(tmp_path, files)
        assert lib.errors == {} and PATIENT in lib.patients

    def test_an_error_from_another_pack_doesnt_override_a_success(self, tmp_path):
        first = clean_files()
        edit_record(first, f"{LIST_STEM}_response.json", body=self.not_found())
        lib = library(tmp_path, first, clean_files())
        assert lib.errors == {} and [s["id"] for s in lib.patients[PATIENT]] == [SURFACE]

    def test_auth_errors_and_non_200s_are_the_emulators_own(self, tmp_path):
        files = clean_files()
        add_call(files, f"integration_surfaces_{OTHER}_20261020-090009.000000", "GET", f"/integration/surfaces/{OTHER}",
                 None, {"body": {"errorData": None, "success": False, "errorCode": "InvalidClientToken",
                                 "errorCodeType": "AuthErrorCodes"}})
        add_call(files, "integration_surfaces_x_20261020-090010.000000", "GET", "/integration/surfaces/x",
                 None, {"body": None}, status=500)
        assert library(tmp_path, files).errors == {}

    def test_a_recorded_unknown_surface_is_indexed(self, tmp_path):
        files = clean_files()
        add_call(files, f"integration_surfaces_{OTHER}_20261020-090009.000000", "GET", f"/integration/surfaces/{OTHER}",
                 None, {"body": {"errorData": None, "success": False, "errorCode": "SurfaceNotFound",
                                 "errorCodeType": "PatientErrorCodes"}})
        assert library(tmp_path, files).errors[(SURFACE_ROUTE, OTHER)].body["errorCode"] == "SurfaceNotFound"

    def test_room_lookups_are_never_indexed(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_rooms_Truebeam_20261020-090009.000000", "GET", "/integration/rooms/Truebeam", None,
                 {"body": {"errorData": None, "success": False, "errorCode": "DuplicateKey",
                           "errorCodeType": "TreatmentRoomAdministrationErrorCodes"}})
        assert library(tmp_path, files).errors == {}

    def test_a_recorded_request_the_route_would_refuse_is_noted(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_GetMap_20261020-090009.000000", "POST", "/integration/GetMap",
                 {"CouchBuffer": 20}, {"body_file": "integration_GetMap_20261020-090009.000000_response.txt"})
        files[f"{PREFIX}integration_GetMap_20261020-090009.000000_response.txt"] = b"a map"
        lib = library(tmp_path, files)
        assert len(lib.maps) == 1
        assert any("1 recorded map or beam-check request(s) aren't ones the route would accept" in notice
                   for notice in lib.notices)

    def test_calls_that_never_got_a_reply_are_noted(self, tmp_path):
        files = clean_files()
        add_call(files, "integration_ping_20261020-090009.000000", "GET", "/integration/ping", None, None)
        assert any("1 recorded call(s) never got a reply" in n for n in library(tmp_path, files).notices)


class TestApplianceVersions:
    """Decision 3 and rule E."""

    def test_the_newest_is_served_with_its_own_api_version(self, tmp_path):
        older = make(tmp_path / "packs", "older", appliance={"server": "Saturn/1.2.288.1", "apiVersion": "1.9.0"})
        newer = make(tmp_path / "packs", "newer", appliance={"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"})
        oldest = make(tmp_path / "packs", "oldest", appliance={"server": "Saturn/1.2.99", "apiVersion": "1.8.0"})
        lib = PackLibrary.build([older, newer, oldest])
        assert lib.appliance == {"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"}
        assert [entry["server"] for entry in lib.appliance_versions] == \
            ["Saturn/1.3.217.2", "Saturn/1.2.288.1", "Saturn/1.2.99"]
        assert lib.warnings == [("Packs were recorded on 3 appliance versions; serving as Saturn/1.3.217.2 "
                                  "(API 2.0.0), the newest. Saturn/1.2.288.1: older. Saturn/1.2.99: oldest.")]

    def test_one_version_gives_no_notice(self, tmp_path):
        lib = library(tmp_path, clean_files(), clean_files())
        assert lib.warnings == []
        assert lib.appliance_versions[0]["packs"] == ["pack-1", "pack-2"]

    def test_the_newest_with_two_api_versions_refuses(self, tmp_path):
        one = make(tmp_path / "packs", "one", appliance={"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"})
        two = make(tmp_path / "packs", "two", appliance={"server": "Saturn/1.3.217.2", "apiVersion": "2.0.1"})
        with pytest.raises(PackConflictError, match="more than one API version") as error:
            PackLibrary.build([one, two])
        assert "one" in str(error.value) and "two" in str(error.value)

    def test_an_older_version_with_two_api_versions_is_only_noted(self, tmp_path):
        packs = [make(tmp_path / "packs", name, appliance=appliance) for name, appliance in (
            ("new", {"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"}),
            ("old-a", {"server": "Saturn/1.2.288.1", "apiVersion": "1.9.0"}),
            ("old-b", {"server": "Saturn/1.2.288.1", "apiVersion": "1.9.1"}))]
        assert PackLibrary.build(packs).appliance["apiVersion"] == "2.0.0"


class TestLoading:
    """Decision 4 and rule B: named packs are refused, scanned ones skipped, and all failing refuses."""

    def folder(self, tmp_path):
        packs = tmp_path / "packs"
        packs.mkdir()
        write_pack(packs / "good.zip", clean_files(), name="good")
        write_pack(packs / "bad.zip", clean_files(), name="bad", anonymized=False)
        return packs

    def test_a_scanned_pack_that_fails_is_skipped(self, tmp_path):
        lib = PackLibrary.load(self.folder(tmp_path))
        assert [pack.name for pack in lib.packs] == ["good"]
        (skipped,) = lib.skipped
        assert skipped.name == "bad" and str(skipped).startswith("bad (") and "not marked anonymized" in str(skipped)
        assert lib.summary().endswith("; skipped 1 pack that failed its checks (see the log)")

    def test_a_named_pack_that_fails_refuses(self, tmp_path):
        with pytest.raises(PacksRefused) as error:
            PackLibrary.load(self.folder(tmp_path), names=["good", "bad"])
        assert [failure.name for failure in error.value.failures] == ["bad"]
        assert "A named pack failed its checks" in str(error.value) and "not marked anonymized" in str(error.value)
        assert str(error.value).endswith(NO_PACKS_HINT)

    def test_a_named_good_pack_loads_alone(self, tmp_path):
        assert [p.name for p in PackLibrary.load(self.folder(tmp_path), names=["good"]).packs] == ["good"]

    def test_every_scanned_pack_failing_refuses(self, tmp_path):
        packs = tmp_path / "packs"
        packs.mkdir()
        write_pack(packs / "a.zip", clean_files(), name="a", anonymized=False)
        (packs / "b.zip").write_bytes(b"not a zip")
        with pytest.raises(PacksRefused) as error:
            PackLibrary.load(packs)
        assert len(error.value.failures) == 2
        assert "Every pack in the packs folder failed its checks" in str(error.value)
        assert str(error.value).endswith(NO_PACKS_HINT)

    def test_a_name_that_matches_no_pack_refuses(self, tmp_path):
        with pytest.raises(PacksRefused, match="is named missing") as error:
            PackLibrary.load(self.folder(tmp_path), names=["missing"])
        assert str(error.value).endswith(NO_PACKS_HINT)

    def test_two_packs_of_one_name_refuse(self, tmp_path):
        packs = self.folder(tmp_path)
        write_pack(packs / "again.zip", clean_files(), name="good")
        with pytest.raises(PacksRefused, match="same name"):
            PackLibrary.load(packs)

    def test_a_conflict_between_scanned_packs_refuses(self, tmp_path):
        packs = tmp_path / "packs"
        packs.mkdir()
        write_pack(packs / "a.zip", clean_files(), name="a")
        second = clean_files()
        edit_record(second, f"{FETCH_STEM}_response.json", body=ok("data:application/octet-stream;base64,AAAA"))
        write_pack(packs / "b.zip", second, name="b")
        with pytest.raises(PackConflictError):
            PackLibrary.load(packs)

    def test_an_empty_folder_loads_nothing(self, tmp_path):
        (tmp_path / "packs").mkdir()
        assert PackLibrary.load(tmp_path / "packs").packs == ()
