"""Reading session packs: a clean pack loads, and each load-time check refuses a seeded one.

Every finding is collected before refusing, so a test can seed several and expect them all in one
:class:`PackError`. The packs are written by ``pack_builder`` (the emulator can't use maprt-pack).
"""

import json

import pytest
from pack_builder import (
    CT_FILE,
    MAP,
    PATIENT,
    ROOM,
    SERVER,
    SESSION,
    SURFACE,
    as_json,
    clean_files,
    ct_slice,
    edit_record,
    record_pair,
    write_pack,
)

from maprt_emulator.packs import (
    Candidate,
    PackDiscoveryError,
    PackError,
    appliance_version,
    discover_packs,
    load_pack,
    read_tree,
)
from maprt_emulator.store import CatalogError

PREFIX = f"sessions/{SESSION}/"
GETMAP = "integration_GetMap_20261020-090005.000000"
SURFACE_FETCH = f"integration_surfaces_{SURFACE}_20261020-090004.000000"


def problems(error):
    return {str(finding) for finding in error.value.findings}


def refused(tmp_path, files, **manifest_changes):
    with pytest.raises(PackError) as error:
        load_pack(write_pack(tmp_path / "pack.zip", files, **manifest_changes))
    return error


class TestACleanPack:
    def test_loads_from_a_zip(self, tmp_path):
        pack = load_pack(write_pack(tmp_path / "pack.zip", clean_files()))
        assert pack.name == "test-pack"
        assert pack.patient_id == PATIENT
        assert (pack.server, pack.server_version, pack.api_version) == (SERVER, (1, 3, 217, 2), "2.0.0")
        assert [session.id for session in pack.sessions] == [SESSION]

    def test_loads_from_a_folder(self, tmp_path):
        (tmp_path / "pack").mkdir()
        pack = load_pack(write_pack(tmp_path / "pack", clean_files(), as_zip=False))
        assert pack.name == "test-pack"

    def test_a_folders_hidden_files_are_not_part_of_it(self, tmp_path):
        (tmp_path / "pack").mkdir()
        write_pack(tmp_path / "pack", clean_files(), as_zip=False)
        (tmp_path / "pack" / ".DS_Store").write_bytes(b"finder")
        assert ".DS_Store" not in read_tree(tmp_path / "pack")
        load_pack(tmp_path / "pack")

    def test_calls_are_in_the_order_sent_with_their_replies(self, tmp_path):
        pack = load_pack(write_pack(tmp_path / "pack.zip", clean_files()))
        paths = [exchange.path for exchange in pack.exchanges]
        assert paths == ["/integration/ping", "/integration/rooms", f"/integration/patients/{PATIENT}/surfaces",
                         f"/integration/surfaces/{SURFACE}", "/integration/GetMap",
                         "/integration/GetBeamDeliveryStatus"]
        rooms = pack.exchanges[1]
        assert rooms.status == 200 and rooms.body["data"][0]["id"] == ROOM and rooms.text is None
        getmap = pack.exchanges[4]
        assert getmap.text == MAP and getmap.body is None
        assert getmap.request_body["CouchBuffer"] == 20
        assert pack.transport_failures == 0

    def test_a_path_is_unquoted(self, tmp_path):
        files = clean_files()
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            "integration_rooms_Truebeam__20261020-090007.000000", "GET", "/integration/rooms/Truebeam%20", None,
            {"body": {"data": ROOM, "success": True, "errorCode": None, "errorCodeType": "NoErrorCodes"}}).items()})
        pack = load_pack(write_pack(tmp_path / "pack.zip", files))
        assert pack.exchanges[-1].path == "/integration/rooms/Truebeam "

    def test_a_session_json_without_format_is_read_as_v1(self, tmp_path):
        files = clean_files()
        info = json.loads(files[PREFIX + "session.json"])
        del info["format"]
        files[PREFIX + "session.json"] = as_json(info)
        load_pack(write_pack(tmp_path / "pack.zip", files))

    def test_pack_error_is_a_catalog_error(self):
        # Startup reports a bad pack the way it reports a bad data folder.
        assert issubclass(PackError, CatalogError)


class TestRepliesThatNeverCame:
    def test_a_request_with_no_response_is_a_transport_failure(self, tmp_path):
        files = clean_files()
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            "integration_ping_20261020-090009.000000", "GET", "/integration/ping", None, None).items()})
        pack = load_pack(write_pack(tmp_path / "pack.zip", files))
        assert pack.exchanges[-1].response is None and pack.exchanges[-1].transport_failure
        assert pack.transport_failures == 1

    def test_a_recorded_transport_error_is_one_too(self, tmp_path):
        files = clean_files()
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            "integration_ping_20261020-090009.000000", "GET", "/integration/ping", None,
            {"body": None, "error": "ReadTimeout: The read operation timed out"}, status=None).items()})
        assert load_pack(write_pack(tmp_path / "pack.zip", files)).transport_failures == 1


class TestBodyRef:
    def test_resolves_to_the_first_copys_body(self, tmp_path):
        files = clean_files()
        second = "integration_surfaces_" + SURFACE + "_20261020-090008.000000"
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            second, "GET", f"/integration/surfaces/{SURFACE}", None,
            {"body_ref": f"{PREFIX}{SURFACE_FETCH}_response.json"}).items()})
        pack = load_pack(write_pack(tmp_path / "pack.zip", files))
        first, again = (e for e in pack.exchanges if e.path == f"/integration/surfaces/{SURFACE}")
        assert again.body == first.body and again.body["data"].startswith("data:")

    def test_resolves_a_map_to_its_text(self, tmp_path):
        files = clean_files()
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            "integration_GetMap_20261020-090008.000000", "POST", "/integration/GetMap", {},
            {"body_ref": f"{PREFIX}{GETMAP}_response.json"}).items()})
        pack = load_pack(write_pack(tmp_path / "pack.zip", files))
        assert pack.exchanges[-1].text == MAP

    def test_a_reference_to_no_file_is_refused(self, tmp_path):
        files = clean_files()
        edit_record(files, f"{SURFACE_FETCH}_response.json", body=None, body_ref=f"{PREFIX}nothing_response.json")
        assert f"{PREFIX}{SURFACE_FETCH}_response.json: body_ref points at no file" in problems(
            refused(tmp_path, files))

    def test_a_chained_reference_is_refused(self, tmp_path):
        files = clean_files()
        files.update({PREFIX + name: as_json(record) for name, record in record_pair(
            "integration_surfaces_x_20261020-090008.000000", "GET", f"/integration/surfaces/{SURFACE}", None,
            {"body_ref": f"{PREFIX}{SURFACE_FETCH}_response.json"}).items()})
        edit_record(files, f"{SURFACE_FETCH}_response.json", body=None,
                    body_ref=f"{PREFIX}integration_surfaces_x_20261020-090008.000000_response.json")
        assert any("body_ref points at another reference" in problem for problem in problems(refused(tmp_path, files)))


class TestTheManifest:
    def test_no_manifest(self, tmp_path):
        (tmp_path / "pack").mkdir()
        (tmp_path / "pack" / "x.txt").write_text("x", encoding="utf-8")
        with pytest.raises(PackError) as error:
            load_pack(tmp_path / "pack")
        assert problems(error) == {"manifest.json: missing"}

    def test_not_a_zip(self, tmp_path):
        (tmp_path / "pack.zip").write_bytes(b"not a zip")
        with pytest.raises(PackError, match="can't be read"):
            load_pack(tmp_path / "pack.zip")

    def test_wrong_format(self, tmp_path):
        assert "manifest.json: format is not maprt-pack/1" in problems(
            refused(tmp_path, clean_files(), format="maprt-pack/0"))

    def test_not_marked_anonymized(self, tmp_path):
        assert "manifest.json: not marked anonymized" in problems(refused(tmp_path, clean_files(), anonymized=None))

    @pytest.mark.parametrize("server", ["Saturn/", "Saturn/1.3.x", "Kestrel", "saturn/1.3", None])  # audit: allow path-construction - Server header values
    def test_an_unreadable_appliance_server(self, tmp_path, server):
        error = refused(tmp_path, clean_files(), appliance={"server": server, "apiVersion": "2.0.0"})
        assert any("doesn't read as Saturn/<numbers>" in problem for problem in problems(error))

    def test_an_api_version_that_isnt_a_string(self, tmp_path):
        error = refused(tmp_path, clean_files(), appliance={"server": SERVER, "apiVersion": 2})
        assert any("apiVersion" in problem for problem in problems(error))

    def test_appliance_version_reads_the_numbers(self):
        assert appliance_version("Saturn/1.3.217.2") > appliance_version("Saturn/1.2.288.1") \
            > appliance_version("Saturn/1.2.99")
        assert appliance_version("Saturn/1.3 beta") is None


PATIENT_SOURCE = {"kind": "patient", "reviewed": "2026-10-30",
                  "labelsAccepted": {"RTPlanLabel": ["Plan01"], "ROIName": ["BODY", "PTV"]}}


class TestTheSource:
    def test_a_pack_without_one_is_source_unknown(self, tmp_path):
        assert load_pack(write_pack(tmp_path / "pack.zip", clean_files())).source is None

    def test_a_patient_pack_keeps_its_block(self, tmp_path):
        pack = load_pack(write_pack(tmp_path / "pack.zip", clean_files(), source=PATIENT_SOURCE))
        assert pack.source == PATIENT_SOURCE

    @pytest.mark.parametrize(("change", "problem"), [
        ({"kind": "cadaver"}, "source.kind 'cadaver' is not one of phantom, patient"),
        ({"reviewed": "30/10/2026"}, "source.reviewed '30/10/2026' isn't a YYYY-MM-DD date"),
        ({"reviewed": None}, "source.reviewed None isn't a YYYY-MM-DD date"),
        ({"labelsAccepted": {"ROIName": "BODY"}}, "source.labelsAccepted isn't a mapping of keywords to lists of text"),
        ({"labelsAccepted": None}, "source.labelsAccepted isn't a mapping of keywords to lists of text"),
    ])
    def test_a_malformed_block_is_refused(self, tmp_path, change, problem):
        error = refused(tmp_path, clean_files(), source={**PATIENT_SOURCE, **change})
        assert f"manifest.json: {problem}" in problems(error)

    def test_a_block_that_isnt_an_object_is_refused(self, tmp_path):
        assert "manifest.json: source isn't a JSON object" in problems(
            refused(tmp_path, clean_files(), source="patient"))


class TestTheFiles:
    def test_a_bad_sha256(self, tmp_path):
        (tmp_path / "pack").mkdir()
        target = write_pack(tmp_path / "pack", clean_files(), as_zip=False)
        (target / PREFIX / "README.md").write_bytes(b"# changed\n")
        with pytest.raises(PackError) as error:
            load_pack(target)
        assert problems(error) == {f"{PREFIX}README.md: sha256 does not match the manifest"}

    def test_an_unlisted_file(self, tmp_path):
        (tmp_path / "pack").mkdir()
        target = write_pack(tmp_path / "pack", clean_files(), as_zip=False)
        (target / "extra.txt").write_text("extra", encoding="utf-8")
        with pytest.raises(PackError) as error:
            load_pack(target)
        assert problems(error) == {"extra.txt: not listed in the manifest's files"}

    def test_a_listed_file_that_is_missing(self, tmp_path):
        (tmp_path / "pack").mkdir()
        target = write_pack(tmp_path / "pack", clean_files(), as_zip=False)
        (target / PREFIX / "README.md").unlink()
        with pytest.raises(PackError) as error:
            load_pack(target)
        assert problems(error) == {f"{PREFIX}README.md: listed in the manifest but missing from the pack"}


class TestTheRecords:
    def test_a_host_in_a_url(self, tmp_path):
        files = clean_files()
        edit_record(files, f"{GETMAP}_request.json", url="https://maprt.site.example:5000/integration/GetMap")
        assert f"{PREFIX}{GETMAP}_request.json: url has a scheme or host" in problems(refused(tmp_path, files))

    def test_a_host_header(self, tmp_path):
        files = clean_files()
        edit_record(files, f"{GETMAP}_request.json", headers={"Host": "maprt.site.example"})
        assert f"{PREFIX}{GETMAP}_request.json: headers carry host" in problems(refused(tmp_path, files))

    def test_an_api_url(self, tmp_path):
        files = clean_files()
        info = json.loads(files[PREFIX + "session.json"])
        info["api_url"] = "https://maprt.site.example:5000"
        files[PREFIX + "session.json"] = as_json(info)
        assert f"{PREFIX}session.json: api_url is set" in problems(refused(tmp_path, files))

    def test_another_patient(self, tmp_path):
        files = clean_files()
        stem = f"integration_patients_{PATIENT}_surfaces_20261020-090003.000000"
        edit_record(files, f"{stem}_request.json", url="/integration/patients/000008/surfaces")
        assert f"{PREFIX}{stem}_request.json: url names another patient" in problems(refused(tmp_path, files))

    def test_a_quoted_other_patient_is_still_found(self, tmp_path):
        files = clean_files()
        stem = f"integration_patients_{PATIENT}_surfaces_20261020-090003.000000"
        edit_record(files, f"{stem}_request.json", url="/integration/patients/ANON%2D1x/surfaces")
        assert f"{PREFIX}{stem}_request.json: url names another patient" in problems(refused(tmp_path, files))

    def test_a_wrong_recording_format(self, tmp_path):
        files = clean_files()
        info = json.loads(files[PREFIX + "session.json"])
        info["format"] = "maprt-recording/2"
        files[PREFIX + "session.json"] = as_json(info)
        assert any("is not maprt-recording/1" in problem for problem in problems(refused(tmp_path, files)))

    def test_a_session_with_no_session_json(self, tmp_path):
        files = clean_files()
        del files[PREFIX + "session.json"]
        assert f"{PREFIX}session.json: missing" in problems(refused(tmp_path, files))

    def test_a_response_with_no_request(self, tmp_path):
        files = clean_files()
        del files[f"{PREFIX}{SURFACE_FETCH}_request.json"]
        assert f"{PREFIX}{SURFACE_FETCH}_response.json: a response with no request" in problems(
            refused(tmp_path, files))

    def test_a_file_that_isnt_a_recording(self, tmp_path):
        files = clean_files()
        files[PREFIX + "notes.json"] = b"{}"
        assert f"{PREFIX}notes.json: isn't a recording file name" in problems(refused(tmp_path, files))

    def test_invalid_json(self, tmp_path):
        files = clean_files()
        files[f"{PREFIX}{GETMAP}_request.json"] = b"{not json"
        assert any(problem.startswith(f"{PREFIX}{GETMAP}_request.json: isn't valid JSON")
                   for problem in problems(refused(tmp_path, files)))

    def test_a_missing_map_text(self, tmp_path):
        files = clean_files()
        del files[f"{PREFIX}{GETMAP}_response.txt"]
        assert any("body_file" in problem for problem in problems(refused(tmp_path, files)))

    def test_no_sessions(self, tmp_path):
        files = {CT_FILE: ct_slice()}
        assert "sessions/: the pack holds no session" in problems(refused(tmp_path, files))


class TestTheDicom:
    def test_not_de_identified(self, tmp_path):
        files = clean_files()
        files[CT_FILE] = ct_slice(removed="NO")
        assert problems(refused(tmp_path, files)) == {f"{CT_FILE}: PatientIdentityRemoved is not YES"}

    def test_another_patient(self, tmp_path):
        files = clean_files()
        files[CT_FILE] = ct_slice(patient_id="000008")
        assert problems(refused(tmp_path, files)) == {f"{CT_FILE}: PatientID is not the manifest's patient"}

    def test_unreadable(self, tmp_path):
        files = clean_files()
        files[CT_FILE] = b"not dicom"
        assert any("isn't a readable DICOM file" in problem for problem in problems(refused(tmp_path, files)))

    def test_a_pack_without_dicom_loads(self, tmp_path):
        load_pack(write_pack(tmp_path / "pack.zip", clean_files(dicom=False)))


def test_every_finding_is_reported_at_once(tmp_path):
    files = clean_files()
    files[CT_FILE] = ct_slice(removed="NO")
    edit_record(files, f"{GETMAP}_request.json", url="https://maprt.site.example/integration/GetMap")
    error = refused(tmp_path, files, anonymized=False)
    assert len(error.value.findings) == 3
    assert error.value.name == "test-pack"
    message = str(error.value)
    assert message.startswith(f"Pack test-pack ({tmp_path / 'pack.zip'}) failed its checks:")
    assert message.count("\n  - ") == 3


class TestDiscovery:
    def test_finds_zips_and_folders_with_a_manifest_not_recursively(self, tmp_path):
        write_pack(tmp_path / "b.zip", clean_files(), name="pack-b")
        (tmp_path / "a").mkdir()
        write_pack(tmp_path / "a", clean_files(), as_zip=False, name="pack-a")
        (tmp_path / "not-a-pack").mkdir()
        (tmp_path / "nested").mkdir()
        write_pack(tmp_path / "nested" / "c.zip", clean_files(), name="pack-c")
        (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
        assert discover_packs(tmp_path) == [Candidate(tmp_path / "a", "pack-a"), Candidate(tmp_path / "b.zip", "pack-b")]

    def test_the_name_is_the_manifests_not_the_files(self, tmp_path):
        write_pack(tmp_path / "file-name.zip", clean_files(), name="manifest-name")
        assert discover_packs(tmp_path, names=["manifest-name"]) == [
            Candidate(tmp_path / "file-name.zip", "manifest-name")]
        with pytest.raises(PackDiscoveryError, match="No pack in .* is named file-name"):
            discover_packs(tmp_path, names=["file-name"])

    def test_names_choose_and_order(self, tmp_path):
        for name in ("one", "two", "three"):
            write_pack(tmp_path / f"{name}.zip", clean_files(), name=name)
        assert [c.name for c in discover_packs(tmp_path, names=["three", "one"])] == ["three", "one"]

    def test_two_packs_with_one_name(self, tmp_path):
        write_pack(tmp_path / "a.zip", clean_files(), name="same")
        write_pack(tmp_path / "b.zip", clean_files(), name="same")
        with pytest.raises(PackDiscoveryError) as error:
            discover_packs(tmp_path)
        assert "same" in str(error.value) and "a.zip" in str(error.value) and "b.zip" in str(error.value)

    def test_an_unreadable_pack_is_found_by_its_path(self, tmp_path):
        (tmp_path / "broken.zip").write_bytes(b"not a zip")
        assert discover_packs(tmp_path) == [Candidate(tmp_path / "broken.zip", None)]

    def test_no_folder(self, tmp_path):
        with pytest.raises(PackDiscoveryError, match="No packs folder"):
            discover_packs(tmp_path / "missing")
