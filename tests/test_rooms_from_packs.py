"""The room routes with packs loaded: the union of the packs' recorded lists, and lookups answered
from it (Round 7 decision B), never from a recorded lookup reply.

Served bodies must be the recorded ones byte for byte, so the list is compared as compact JSON.
"""

import json

import pytest
from pack_builder import ROOM, SESSION, clean_files, edit_record, write_pack

from maprt_emulator.app import create_app
from maprt_emulator.store import EMPTY_GUID, CatalogError

ROOMS_STEM = "integration_rooms_20261020-090002.000000"
OTHER = "0a0b0c0d-1111-4222-8333-444455556666"
EMPTY_NAME = "1b2c3d4e-1111-4222-8333-444455556666"


def room(guid, machine_name, name="TB"):
    return {"name": name, "id": guid, "machineName": machine_name, "coordinateSystem": "IEC_61217"}


def with_rooms(rooms):
    files = clean_files()
    body = json.loads(files[f"sessions/{SESSION}/{ROOMS_STEM}_response.json"])["body"]
    body["data"] = rooms
    edit_record(files, f"{ROOMS_STEM}_response.json", body=body)
    return files


RECORDED = [room(ROOM, "Truebeam", "TB1"), room(OTHER, "Truebeam", "TB1 e"), room("2c" + ROOM[2:], "Truebeam "),
            room(EMPTY_NAME, "")]


@pytest.fixture
def packs_dir(tmp_path):
    folder = tmp_path / "packs"
    folder.mkdir()
    write_pack(folder / "a.zip", with_rooms(RECORDED), name="a")
    return folder


def compact(value):
    return json.dumps(value, separators=(",", ":")).encode()


class TestFromPacks:
    def test_the_list_is_the_recorded_one_byte_for_byte(self, client_factory, packs_dir):
        with client_factory(packs_dir=packs_dir) as client:
            reply = client.get("/integration/rooms")
        assert reply.content == compact({"data": RECORDED, "errorData": None, "success": True, "errorCode": None,
                                         "errorCodeType": "NoErrorCodes"})

    def test_the_union_of_two_packs_in_first_seen_order(self, client_factory, packs_dir):
        write_pack(packs_dir / "b.zip", with_rooms([room("3d" + ROOM[2:], "Edge"), RECORDED[0]]), name="b")
        with client_factory(packs_dir=packs_dir) as client:
            ids = [r["id"] for r in client.get("/integration/rooms").json()["data"]]
        assert ids == [r["id"] for r in RECORDED] + ["3d" + ROOM[2:]]

    @pytest.mark.parametrize(("name", "expected"), [
        ("Truebeam ", "2c" + ROOM[2:]),        # a trailing space is part of the name
        ("Unknown", EMPTY_GUID),                # no match: success, the all-zeros GUID
        ("truebeam", EMPTY_GUID),               # case counts
    ])
    def test_a_lookup_answers_from_the_union(self, client_factory, packs_dir, name, expected):
        with client_factory(packs_dir=packs_dir) as client:
            reply = client.get(f"/integration/rooms/{name}").json()
        assert reply["success"] is True and reply["data"] == expected

    def test_a_shared_machine_name_is_a_duplicate_key(self, client_factory, packs_dir):
        with client_factory(packs_dir=packs_dir) as client:
            reply = client.get("/integration/rooms/Truebeam").json()
        assert reply == {"errorData": None, "success": False, "errorCode": "DuplicateKey",
                         "errorCodeType": "TreatmentRoomAdministrationErrorCodes"}

    def test_the_catalogs_rooms_are_not_served(self, client_factory, packs_dir):
        with client_factory(packs_dir=packs_dir) as client:
            ids = {r["id"] for r in client.get("/integration/rooms").json()["data"]}
        assert ids == {r["id"] for r in RECORDED}


class TestWithoutPacks:
    def catalog_rooms(self, client_factory, **settings):
        with client_factory(**settings) as client:
            return client.get("/integration/rooms").json()["data"]

    def test_no_packs_folder_serves_the_catalog(self, client_factory):
        assert len(self.catalog_rooms(client_factory)) == 4

    def test_no_packs_ignores_a_packs_folder(self, client_factory, packs_dir):
        assert len(self.catalog_rooms(client_factory, packs_dir=packs_dir, no_packs=True)) == 4

    def test_an_empty_packs_folder_serves_the_catalog(self, client_factory, tmp_path):
        (tmp_path / "empty").mkdir()
        assert len(self.catalog_rooms(client_factory, packs_dir=tmp_path / "empty")) == 4

    def test_naming_packs_without_a_folder_refuses(self, make_settings):
        with pytest.raises(CatalogError, match="no packs folder was given"):
            create_app(make_settings(packs=("a",)))

    def test_a_bad_named_pack_refuses_startup(self, make_settings, packs_dir):
        write_pack(packs_dir / "bad.zip", clean_files(), name="bad", anonymized=False)
        with pytest.raises(CatalogError, match="--no-packs"):
            create_app(make_settings(packs_dir=packs_dir, packs=("bad",)))


def test_the_settings_survive_the_environment(make_settings, packs_dir):
    # --reload passes settings through the environment, so the new fields must round-trip.
    env = make_settings(packs_dir=packs_dir, packs=("a", "b"), no_packs=True).as_env()
    assert env["MAPRT_EMULATOR_PACKS_DIR"] == str(packs_dir)
    assert json.loads(env["MAPRT_EMULATOR_PACKS"]) == ["a", "b"]
    assert env["MAPRT_EMULATOR_NO_PACKS"] == "true"
