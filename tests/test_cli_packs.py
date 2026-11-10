"""The packs on the command line: the flags, the banner and server.json, the appliance presented,
and ``--check-data``'s packs section."""

import json

import pytest
from pack_builder import SESSION, SURFACE, clean_files, write_pack

from maprt_emulator.app import create_app
from maprt_emulator.check import Report, check_packs
from maprt_emulator.check import main as check_main
from maprt_emulator.cli import _parser, banner, main, settings_from_args
from maprt_emulator.library import NO_PACKS_HINT, PackLibrary
from maprt_emulator.store import Catalog

FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"


def parse(*argv):
    return settings_from_args(_parser().parse_args(argv))


@pytest.fixture(autouse=True)
def no_pack_environment(monkeypatch):
    for name in ("MAPRT_EMULATOR_PACKS_DIR", "MAPRT_EMULATOR_PACKS", "MAPRT_EMULATOR_NO_PACKS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def packs_dir(tmp_path):
    folder = tmp_path / "packs"
    folder.mkdir()
    unfetched = clean_files()
    del unfetched[f"sessions/{SESSION}/{FETCH_STEM}_request.json"]
    del unfetched[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
    write_pack(folder / "good.zip", unfetched, name="good")
    write_pack(folder / "bad.zip", clean_files(), name="bad", anonymized=False)
    return folder


class TestFlags:
    def test_packs_dir_and_repeated_pack(self, tmp_path):
        settings = parse("--packs-dir", str(tmp_path), "--pack", "a", "--pack", "b")
        assert (settings.packs_dir, settings.packs, settings.no_packs) == (tmp_path, ("a", "b"), False)

    def test_no_packs(self):
        assert parse("--no-packs").no_packs is True

    @pytest.mark.parametrize("extra", [["--packs-dir", "x"], ["--pack", "a"]])
    def test_no_packs_with_packs_is_a_usage_error(self, extra):
        with pytest.raises(SystemExit, match="--no-packs reads no pack"):
            parse("--no-packs", *extra)

    def test_no_packs_beats_a_packs_folder_in_the_environment(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAPRT_EMULATOR_PACKS_DIR", str(tmp_path))
        settings = parse("--no-packs")
        assert settings.packs_dir == tmp_path and settings.no_packs is True

    def test_the_environment_alone_chooses_packs(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAPRT_EMULATOR_PACKS_DIR", str(tmp_path))
        monkeypatch.setenv("MAPRT_EMULATOR_PACKS", '["a"]')
        settings = parse()
        assert (settings.packs_dir, settings.packs) == (tmp_path, ("a",))


class TestStartingRefused:
    def test_a_named_pack_that_fails_stops_at_the_prompt_with_the_hint(self, packs_dir, tmp_path):
        with pytest.raises(SystemExit) as error:
            main(["--packs-dir", str(packs_dir), "--pack", "bad", "--state-dir", str(tmp_path / "state")])
        assert "not marked anonymized" in str(error.value) and NO_PACKS_HINT in str(error.value)
        assert not (tmp_path / "state" / "token").exists()


class TestBanner:
    def text(self, settings, data_dir, library):
        return banner(settings, Catalog.load(data_dir), settings.state_dir / "token", library=library)

    def test_shows_the_packs_their_folder_and_the_appliance(self, make_settings, data_dir, packs_dir):
        settings = make_settings(packs_dir=packs_dir)
        text = self.text(settings, data_dir, PackLibrary.load(packs_dir))
        assert "Packs       1 pack, 1 patient, 0 surfaces" in text
        assert "1 beam check" in text
        assert "1 listed surface has no recorded mesh" in text
        assert "Appliance   Saturn/1.3.217.2 (API 2.0.0)" in text
        assert "  Data  " not in text

    def test_reports_skipped_packs_and_notices_below_the_box(self, make_settings, data_dir, packs_dir):
        text = self.text(make_settings(packs_dir=packs_dir), data_dir, PackLibrary.load(packs_dir))
        below = text.split("╯", 1)[1]
        assert "Skipped bad" in below and "not marked anonymized" in below
        assert "listed but never fetched" in below

    def test_the_box_is_not_ragged(self, make_settings, data_dir, packs_dir):
        text = self.text(make_settings(packs_dir=packs_dir), data_dir, PackLibrary.load(packs_dir))
        lines = [line for line in text.splitlines() if line.startswith(("╭", "│", "╰"))]
        assert len({len(line) for line in lines}) == 1

    def test_no_packs_says_so(self, make_settings, data_dir, packs_dir):
        text = self.text(make_settings(packs_dir=packs_dir, no_packs=True), data_dir, None)
        assert "Packs       off (--no-packs)" in text and "3 patients" in text

    def test_an_empty_folder_says_so(self, make_settings, data_dir, tmp_path):
        (tmp_path / "empty").mkdir()
        library = PackLibrary.load(tmp_path / "empty")
        text = self.text(make_settings(packs_dir=tmp_path / "empty"), data_dir, library)
        assert "Packs       none in" in text and "3 patients" in text

    def test_mixed_appliance_versions_are_warned(self, make_settings, data_dir, packs_dir):
        write_pack(packs_dir / "old.zip", clean_files(), name="old",
                   appliance={"server": "Saturn/1.2.288.1", "apiVersion": "1.9.0"})
        text = self.text(make_settings(packs_dir=packs_dir), data_dir, PackLibrary.load(packs_dir))
        assert "packs from several versions" in text and "Saturn/1.2.288.1: old." in text


def test_a_long_summary_wraps_at_its_separators():
    from maprt_emulator.cli import _wrap
    lines = _wrap("2 packs, 2 patients, 1 surface, 16 rooms, 1 map, 1 beam check; 2 listed surfaces have no mesh", 40)
    assert all(len(line) <= 40 for line in lines)
    assert " ".join(lines).startswith("2 packs, 2 patients, 1 surface,")
    assert lines[-1] == "2 listed surfaces have no mesh"


class TestServerJson:
    def test_holds_the_packs_block(self, make_settings, packs_dir):
        settings = make_settings(packs_dir=packs_dir)
        settings.write_token(packs=PackLibrary.load(packs_dir).report())
        packs = json.loads(settings.server_path.read_text(encoding="utf-8"))["packs"]
        assert [pack["name"] for pack in packs["loaded"]] == ["good"]
        assert packs["skipped"][0]["name"] == "bad" and "not marked anonymized" in packs["skipped"][0]["findings"][0]
        assert packs["appliance"] == {"server": "Saturn/1.3.217.2", "apiVersion": "2.0.0"}
        assert packs["unfetched"] == [{"surface": SURFACE, "patient": "ANON-1", "packs": ["good"]}]

    def test_has_no_packs_block_without_packs(self, make_settings):
        settings = make_settings()
        settings.write_token()
        assert "packs" not in json.loads(settings.server_path.read_text(encoding="utf-8"))


class TestTheApplianceServed:
    def test_is_the_packs_in_the_server_header_and_ping(self, client_factory, tmp_path):
        folder = tmp_path / "old"
        folder.mkdir()
        write_pack(folder / "a.zip", clean_files(), name="a",
                   appliance={"server": "Saturn/1.2.288.1", "apiVersion": "1.9.0"})
        with client_factory(packs_dir=folder) as client:
            reply = client.get("/integration/ping")
        assert reply.headers["server"] == "Saturn/1.2.288.1" and reply.json()["data"] == "1.9.0"

    def test_is_the_built_in_one_without_packs(self, client_factory):
        with client_factory() as client:
            reply = client.get("/integration/ping")
        assert reply.headers["server"] == "Saturn/1.3.217.2" and reply.json()["data"] == "2.0.0"

    def test_a_library_handed_in_is_not_loaded_again(self, make_settings, packs_dir):
        library = PackLibrary.load(packs_dir)
        assert create_app(make_settings(packs_dir=packs_dir / "nowhere"), library).state.library is library


class TestCheckData:
    def test_a_skipped_pack_is_an_error(self, make_settings, packs_dir):
        report = Report()
        check_packs(make_settings(packs_dir=packs_dir), report)
        assert any("bad" in error and "would be skipped" in error for error in report.errors)
        assert any("listed but never fetched" in note for note in report.notes)

    def test_a_refusal_is_an_error(self, make_settings, packs_dir):
        report = Report()
        check_packs(make_settings(packs_dir=packs_dir, packs=("missing",)), report)
        assert any("is named missing" in error for error in report.errors)

    def test_clean_packs_exit_zero(self, make_settings, data_dir, tmp_path, capsys):
        folder = tmp_path / "clean"
        folder.mkdir()
        write_pack(folder / "a.zip", clean_files(), name="a")
        assert check_main(data_dir, make_settings(packs_dir=folder)) == 0
        assert "packs: 1 pack" in capsys.readouterr().out

    def test_a_skipped_pack_exits_non_zero(self, make_settings, data_dir, packs_dir):
        assert check_main(data_dir, make_settings(packs_dir=packs_dir)) == 1

    def test_no_packs_is_a_note(self, make_settings):
        report = Report()
        check_packs(make_settings(no_packs=True), report)
        assert report.notes == ["packs: off (--no-packs)"] and report.ok
