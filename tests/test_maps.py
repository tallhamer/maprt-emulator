"""The recorded-map library: matching, nearest-fallback, the overlay, and load errors."""

import json

import pytest

from maprt_emulator.maps import MapLibrary, MapLibraryError
from maprt_emulator.schemas import GetMapRequest

SURFACE = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"
ROOM = "96bbc235-0da9-e983-0783-5973a7d11144"


def request(**overrides):
    body = {"PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "PatientPosition": "HFS",
            "CouchBuffer": 20.0, "PatientBuffer": 20.0, "HighResolution": False,
            "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0}}
    iso = overrides.pop("isocenter", None)
    if iso is not None:
        body["Isocenter"] = {"x": iso[0], "y": iso[1], "z": iso[2]}
    body.update(overrides)
    return GetMapRequest.model_validate(body)


class TestLoading:
    def test_loads_the_shipped_recording(self, data_dir):
        assert len(MapLibrary.load(data_dir)) == 1

    def test_a_folder_with_no_maps_directory_loads_empty(self, tmp_path):
        # Not an error: a data folder need not have any recordings, and the model covers it.
        assert len(MapLibrary.load(tmp_path)) == 0

    def test_a_missing_index_loads_empty(self, data_dir):
        (data_dir / "maps" / "index.json").unlink()
        assert len(MapLibrary.load(data_dir)) == 0


class TestMatching:
    def test_the_exact_setup_matches(self, data_dir):
        recording, source = MapLibrary.load(data_dir).resolve(request())
        assert source == "recorded"
        assert recording.path.name == "demo_hfs_lowres.txt"

    def test_a_tiny_isocenter_difference_still_counts_as_exact(self, data_dir):
        # Exact float equality would make a match unreachable from a hand-typed notebook cell.
        _recording, source = MapLibrary.load(data_dir).resolve(request(isocenter=(0.1, 0.0, 0.2)))
        assert source == "recorded"

    def test_a_real_isocenter_difference_falls_back_to_nearest(self, data_dir):
        _recording, source = MapLibrary.load(data_dir).resolve(request(isocenter=(0, 0, 80)))
        assert source == "recorded-nearest"

    @pytest.mark.parametrize("difference", [
        {"CouchBuffer": 30.0},
        {"PatientBuffer": 30.0},
        {"PatientPosition": "FFS"},
    ])
    def test_buffers_and_position_must_match_for_an_exact_hit(self, data_dir, difference):
        _recording, source = MapLibrary.load(data_dir).resolve(request(**difference))
        # Still the same surface/room/resolution, so it is a nearest candidate — but not exact.
        assert source == "recorded-nearest"

    def test_a_different_resolution_is_not_even_a_nearest_candidate(self, data_dir):
        # A coarse recording is not a near-enough answer to a high-resolution question.
        recording, source = MapLibrary.load(data_dir).resolve(request(HighResolution=True))
        assert (recording, source) == (None, None)

    def test_a_different_surface_is_not_a_candidate(self, data_dir):
        recording, _source = MapLibrary.load(data_dir).resolve(
            request(PatientSurfaceId="a6f3fbee-6714-4f92-80c1-f5aadda4cbeb"))
        assert recording is None

    def test_a_different_room_is_not_a_candidate(self, data_dir):
        recording, _source = MapLibrary.load(data_dir).resolve(request(TreatmentRoomId="other"))
        assert recording is None

    def test_nearest_can_be_refused(self, data_dir):
        recording, source = MapLibrary.load(data_dir).resolve(
            request(isocenter=(0, 0, 80)), allow_nearest=False)
        assert (recording, source) == (None, None)

    def test_the_nearest_of_several_wins(self, data_dir):
        maps_dir = data_dir / "maps"
        entries = json.loads((maps_dir / "index.json").read_text(encoding="utf-8"))
        body = (maps_dir / entries[0]["file"]).read_text(encoding="utf-8")
        for distance in (50, 300):
            (maps_dir / f"at{distance}.txt").write_text(body, encoding="utf-8")
            entries.append({**entries[0], "file": f"at{distance}.txt",
                            "isocenter": [0, 0, distance]})
        (maps_dir / "index.json").write_text(json.dumps(entries), encoding="utf-8")

        recording, source = MapLibrary.load(data_dir).resolve(request(isocenter=(0, 0, 320)))
        assert source == "recorded-nearest"
        assert recording.path.name == "at300.txt"


class TestOverlay:
    def test_a_local_recording_beats_a_shipped_one_for_the_same_setup(self, data_dir):
        # The mechanism that lets a real recording take over from a synthetic snapshot without
        # the snapshot being deleted — and without the real one being a commit candidate.
        local = data_dir / "local" / "maps"
        local.mkdir(parents=True)
        (local / "real.txt").write_text("2,2 270,180,1 270,0,1 0,180,1 0,0,1 DONE\n",
                                        encoding="utf-8")
        entries = json.loads((data_dir / "maps" / "index.json").read_text(encoding="utf-8"))
        (local / "index.json").write_text(
            json.dumps([{**entries[0], "file": "real.txt", "note": "the real one"}]),
            encoding="utf-8")

        recording, source = MapLibrary.load(data_dir).resolve(request())
        assert source == "recorded"
        assert recording.path.name == "real.txt"


class TestLoadErrors:
    def test_a_listed_file_that_does_not_exist_is_reported(self, data_dir):
        index = data_dir / "maps" / "index.json"
        entries = json.loads(index.read_text(encoding="utf-8"))
        entries[0]["file"] = "gone.txt"
        index.write_text(json.dumps(entries), encoding="utf-8")
        with pytest.raises(MapLibraryError, match="gone.txt"):
            MapLibrary.load(data_dir)

    def test_a_missing_key_names_the_key(self, data_dir):
        index = data_dir / "maps" / "index.json"
        entries = json.loads(index.read_text(encoding="utf-8"))
        del entries[0]["surfaceId"]
        index.write_text(json.dumps(entries), encoding="utf-8")
        with pytest.raises(MapLibraryError, match="surfaceId"):
            MapLibrary.load(data_dir)

    def test_bad_json_names_the_file(self, data_dir):
        (data_dir / "maps" / "index.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(MapLibraryError, match="index.json"):
            MapLibrary.load(data_dir)

    def test_a_malformed_isocenter_is_reported(self, data_dir):
        index = data_dir / "maps" / "index.json"
        entries = json.loads(index.read_text(encoding="utf-8"))
        entries[0]["isocenter"] = [0, 0]
        index.write_text(json.dumps(entries), encoding="utf-8")
        with pytest.raises(MapLibraryError, match="x, y, z"):
            MapLibrary.load(data_dir)


class TestSummary:
    def test_singular_for_one(self, data_dir):
        assert MapLibrary.load(data_dir).summary() == "1 recorded map"

    def test_plural_for_none(self, tmp_path):
        assert MapLibrary.load(tmp_path).summary() == "0 recorded maps"
