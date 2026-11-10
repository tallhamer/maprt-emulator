"""The inventory page (Phase 6 step 8): /inventory, /inventory.md and ``maprt-emulator inventory``."""

import json
import re

import pytest
from pack_builder import MAP, ROOM, SESSION, SURFACE, clean_files, edit_record, write_pack

from maprt_emulator.cli import main

FETCH_STEM = f"integration_surfaces_{SURFACE}_20261020-090004.000000"
ROOMS_STEM = "integration_rooms_20261020-090002.000000"


@pytest.fixture
def packs_dir(tmp_path):
    """Two packs from two appliance versions, one with a surface never fetched, and one that fails."""
    folder = tmp_path / "packs"
    folder.mkdir()
    write_pack(folder / "full.zip", clean_files(), name="full", description="every call",
               source={"kind": "patient", "reviewed": "2026-10-30",
                       "labelsAccepted": {"RTPlanLabel": ["Plan01"], "ROIName": ["BODY", "PTV"]}})
    unfetched = clean_files()
    del unfetched[f"sessions/{SESSION}/{FETCH_STEM}_request.json"]
    del unfetched[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
    write_pack(folder / "older.zip", unfetched, name="older",
               appliance={"server": "Saturn/1.2.288.1", "apiVersion": "1.9.0"})
    write_pack(folder / "bad.zip", clean_files(), name="bad", anonymized=False)
    return folder


def page(client_factory, **settings):
    with client_factory(token=None, map_seconds=0.0, **settings) as client:
        return client.get("/inventory.md"), client.get("/inventory")  # audit: allow writable-locations - a URL route, not a file


def code_blocks(markdown, heading):
    section = markdown.split(f"## {heading}", 1)[1].split("\n## ", 1)[0]
    return [json.loads(block) for block in re.findall(r"```json\n(.*?)\n```", section, re.DOTALL)]


class TestTheRoutes:
    def test_answer_without_a_token_with_their_media_types(self, client_factory, packs_dir):
        markdown, html = page(client_factory, packs_dir=packs_dir)
        assert markdown.status_code == html.status_code == 200
        assert markdown.headers["content-type"].startswith("text/markdown")
        assert html.headers["content-type"].startswith("text/html")
        assert html.text.startswith("<!doctype html>") and "<h1>MapRT Emulator inventory</h1>" in html.text

    def test_are_not_in_the_api_schema(self, client_factory):
        with client_factory() as client:
            schema = client.get("/openapi.json").json()  # audit: allow writable-locations - a URL route, not a file
        assert not any(path.startswith("/inventory") for path in schema["paths"])
        assert "(/inventory)" in schema["info"]["description"]


class TestWhatItLists:
    def test_the_packs_skipped_warned_and_unfetched(self, client_factory, packs_dir):
        text = page(client_factory, packs_dir=packs_dir)[0].text
        assert "### full" in text and "every call" in text and "### older" in text
        assert "## Skipped packs" in text and "`bad`" in text and "not marked anonymized" in text
        assert "**Warning:** Packs were recorded on 2 appliance versions" in text
        assert "Presented as appliance **Saturn/1.3.217.2**, API **2.0.0**" in text
        assert "fetched |" in text

    def test_each_packs_source(self, client_factory, packs_dir, tmp_path):
        text = page(client_factory, packs_dir=packs_dir)[0].text
        full, older = text.split("### full", 1)[1].split("### older", 1)
        assert ("Source: **patient dataset**, labels reviewed 2026-10-30; 3 labels accepted as written "
                "(RTPlanLabel, ROIName)") in full
        assert "Source unknown (the manifest doesn't say)" in older

        folder = tmp_path / "phantom"
        folder.mkdir()
        write_pack(folder / "p.zip", clean_files(), name="p",
                   source={"kind": "phantom", "reviewed": "2026-10-30", "labelsAccepted": {}})
        assert "Source: phantom, saved 2026-10-30; no labels to review" in page(client_factory, packs_dir=folder)[0].text

    def test_a_surface_listed_but_never_fetched(self, client_factory, tmp_path):
        folder = tmp_path / "one"
        folder.mkdir()
        files = clean_files()
        del files[f"sessions/{SESSION}/{FETCH_STEM}_request.json"], files[f"sessions/{SESSION}/{FETCH_STEM}_response.json"]
        write_pack(folder / "a.zip", files, name="a")
        assert "**listed but never fetched**: answers `SurfaceNotFound`" in page(client_factory, packs_dir=folder)[0].text

    def test_rooms_with_what_a_lookup_answers(self, client_factory, tmp_path):
        folder = tmp_path / "rooms"
        folder.mkdir()
        files = clean_files()
        rooms = [{"name": "A", "id": ROOM, "machineName": "TB", "coordinateSystem": "IEC_61217"},
                 {"name": "B", "id": "0a" + ROOM[2:], "machineName": "TB", "coordinateSystem": "IEC_61217"},
                 {"name": "C <&>", "id": "0b" + ROOM[2:], "machineName": "TB ", "coordinateSystem": "IEC_61217"}]
        edit_record(files, f"{ROOMS_STEM}_response.json", body={"data": rooms, "errorData": None, "success": True,
                                                                "errorCode": None, "errorCodeType": "NoErrorCodes"})
        write_pack(folder / "a.zip", files, name="a")
        markdown, html = page(client_factory, packs_dir=folder)
        assert '| A | `"TB"` |' in markdown.text and "`DuplicateKey` (2 rooms)" in markdown.text
        assert f'| C <&> | `"TB "` | `0b{ROOM[2:]}` | `0b{ROOM[2:]}` |' in markdown.text
        assert "C &lt;&amp;&gt;" in html.text and "C <&>" not in html.text

    def test_without_packs_the_catalog(self, client_factory):
        text = page(client_factory)[0].text
        assert "No packs folder (`--packs-dir`): serving the catalog." in text and "ANON001" in text

    def test_no_packs_says_so(self, client_factory, packs_dir):
        assert "Packs are off (`--no-packs`)" in page(client_factory, packs_dir=packs_dir, no_packs=True)[0].text


class TestCopyReadyBodies:
    def test_each_gets_its_recorded_reply(self, client_factory, tmp_path):
        folder = tmp_path / "one"
        folder.mkdir()
        files = clean_files()
        write_pack(folder / "a.zip", files, name="a")
        with client_factory(packs_dir=folder, map_seconds=0.0, source_headers=True) as client:
            text = client.get("/inventory.md").text  # audit: allow writable-locations - a URL route, not a file
            maps, beams = code_blocks(text, "GetMap: recorded requests"), code_blocks(text, "GetBeamDeliveryStatus")
            assert len(maps) == len(beams) == 1
            for body in maps:
                reply = client.post("/integration/GetMap", json=body)
                assert reply.content == MAP and reply.headers["x-emulator-map-source"] == "recorded"
            for body in beams:
                reply = client.post("/integration/GetBeamDeliveryStatus", json=body)
                assert reply.headers["x-emulator-beam-source"] == "recorded"


def test_the_command_prints_the_same_markdown(client_factory, data_dir, packs_dir, tmp_path, capsys, monkeypatch):
    for name in ("MAPRT_EMULATOR_PACKS_DIR", "MAPRT_EMULATOR_PACKS", "MAPRT_EMULATOR_NO_PACKS"):
        monkeypatch.delenv(name, raising=False)
    served = page(client_factory, packs_dir=packs_dir)[0].text
    main(["inventory", "--packs-dir", str(packs_dir), "--state-dir", str(tmp_path / "state"),
          "--data-dir", str(data_dir)])
    assert capsys.readouterr().out == served


def test_the_command_refuses_like_the_server(packs_dir, tmp_path):
    with pytest.raises(SystemExit, match="--no-packs"):
        main(["inventory", "--packs-dir", str(packs_dir), "--pack", "bad", "--state-dir", str(tmp_path / "s")])
