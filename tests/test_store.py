"""The data catalog: loading, the ``data/MapRT/local`` overlay, and startup error messages.

A data problem has to be a clear message at the prompt, not a traceback inside a worker during
someone's first notebook cell — so these check the messages as well as the behaviour.
"""

import json

import pytest

from maprt_emulator.settings import BUILTIN_DATA_DIR, find_data_dir
from maprt_emulator.store import Catalog, CatalogError


def test_loads_the_shipped_catalog(data_dir):
    catalog = Catalog.load(data_dir)
    assert len(catalog.rooms) == 4
    assert catalog.known_patients == {"ANON001", "ANON002", "ANON003"}


class TestPatientsWithoutSurfaces:
    def test_a_patient_with_an_empty_list_survives_loading(self, data_dir):
        # The bug this guards: building the patient index from the surfaces alone drops any
        # patient who has none, turning "known, nothing acquired" into "unknown patient".
        catalog = Catalog.load(data_dir)
        assert catalog.knows_patient("ANON003")
        assert catalog.surfaces_for("ANON003") == []

    def test_and_is_counted_in_the_banner_summary(self, data_dir):
        assert Catalog.load(data_dir).summary().startswith("3 patients")


class TestRoomLookups:
    def test_a_shared_machine_name_returns_every_match(self, data_dir):
        assert len(Catalog.load(data_dir).rooms_by_machine_name("TrueBeam")) == 2

    def test_a_unique_machine_name_returns_one(self, data_dir):
        assert len(Catalog.load(data_dir).rooms_by_machine_name("TrueBeam2")) == 1

    def test_an_unknown_machine_name_returns_none(self, data_dir):
        assert Catalog.load(data_dir).rooms_by_machine_name("Nope") == []

    def test_rooms_can_be_found_by_guid(self, data_dir):
        catalog = Catalog.load(data_dir)
        room = catalog.room_by_id("b7e4a910-5f28-4c63-8d11-6a9307e4c250")
        assert room.name == "Halcyon Room 3"


class TestSurfaceOrdering:
    def test_newest_first(self, data_dir):
        surfaces = Catalog.load(data_dir).surfaces_for("ANON001")
        assert surfaces[0].timestamp > surfaces[1].timestamp


class TestOverlay:
    def test_a_local_rooms_file_replaces_the_public_one(self, data_dir):
        local = data_dir / "local"
        local.mkdir()
        (local / "rooms.json").write_text(json.dumps([
            {"id": "x", "machineName": "OnlyRoom", "name": "Only", "coordinateSystem": "IEC_61217"}
        ]), encoding="utf-8")
        catalog = Catalog.load(data_dir)
        assert [room.machine_name for room in catalog.rooms] == ["OnlyRoom"]

    def test_a_local_surface_wins_over_the_public_one_of_the_same_id(self, data_dir):
        surface_id = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"
        local_surfaces = data_dir / "local" / "surfaces"
        local_surfaces.mkdir(parents=True)
        (local_surfaces / f"{surface_id}.obj").write_text(
            "v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 3\n", encoding="utf-8")
        catalog = Catalog.load(data_dir)
        # This is the mechanism that keeps real patient surfaces out of the repository while
        # still letting them be served on a machine that has them.
        assert catalog.surface(surface_id).path.parent == local_surfaces


class TestStartupErrors:
    def test_a_missing_data_directory_names_the_path_and_the_flag(self, tmp_path):
        with pytest.raises(CatalogError, match="--data-dir"):
            Catalog.load(tmp_path / "nowhere")

    def test_a_missing_rooms_file_is_reported(self, data_dir):
        (data_dir / "rooms.json").unlink()
        with pytest.raises(CatalogError, match="rooms.json"):
            Catalog.load(data_dir)

    def test_a_room_missing_a_field_names_the_field(self, data_dir):
        (data_dir / "rooms.json").write_text(json.dumps([{"id": "x", "name": "y"}]),
                                             encoding="utf-8")
        with pytest.raises(CatalogError, match="machineName"):
            Catalog.load(data_dir)

    def test_an_empty_rooms_file_is_reported(self, data_dir):
        (data_dir / "rooms.json").write_text("[]", encoding="utf-8")
        with pytest.raises(CatalogError, match="no treatment rooms"):
            Catalog.load(data_dir)

    def test_a_surface_listed_without_a_file_names_what_was_searched(self, data_dir):
        catalog = json.loads((data_dir / "patients.json").read_text(encoding="utf-8"))
        catalog["ANON001"].append({"id": "missing-surface", "timeStamp": "2026-01-01T00:00:00",
                                   "timeZone": "UTC"})
        (data_dir / "patients.json").write_text(json.dumps(catalog), encoding="utf-8")
        with pytest.raises(CatalogError, match="missing-surface"):
            Catalog.load(data_dir)


class TestFindingTheDataDirectory:
    """``data/`` holds one folder per Vision RT product; the default is MapRT's."""

    @pytest.fixture
    def repo(self, tmp_path):
        product = tmp_path / "data" / "MapRT"
        product.mkdir(parents=True)
        (product / "rooms.json").write_text("[]", encoding="utf-8")
        (tmp_path / "notebooks").mkdir()
        return tmp_path

    def test_it_finds_the_maprt_folder_from_the_root(self, repo):
        assert find_data_dir(repo) == (repo / "data" / "MapRT").resolve()

    def test_it_walks_up_from_a_subdirectory(self, repo):
        assert find_data_dir(repo / "notebooks") == (repo / "data" / "MapRT").resolve()

    def test_a_catalog_directly_under_data_is_not_mistaken_for_it(self, tmp_path):
        # The pre-2026-10-18 layout. Serving it would mean serving another product's folder.
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "rooms.json").write_text("[]", encoding="utf-8")
        found = find_data_dir(tmp_path)
        assert found != (tmp_path / "data").resolve()
        assert found == BUILTIN_DATA_DIR

    def test_with_no_data_folder_above_it_serves_the_built_in_catalog(self, tmp_path):
        # So `pip install` then `maprt-emulator` works anywhere, with no data of your own.
        assert find_data_dir(tmp_path) == BUILTIN_DATA_DIR
        assert (BUILTIN_DATA_DIR / "rooms.json").is_file()
