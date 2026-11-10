"""Tests for get_packs.py, the pack downloader at the root of the published repo.

GitHub is replaced by a fake opener, so nothing touches the network. The script isn't part of the
installed package, so it's loaded from its file: one folder above these tests, both here
(`apps/maprt-emulator/`) and in the published repo (its root).
"""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "get_packs.py"  # audit: allow writable-locations - read only, the script under test

spec = importlib.util.spec_from_file_location("get_packs", SCRIPT)
get_packs = importlib.util.module_from_spec(spec)
sys.modules["get_packs"] = get_packs
spec.loader.exec_module(get_packs)

REPO = "tallhamer/maprt-emulator"
TAG = "packs-v1"
API_URL = f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"
DL = f"https://github.com/{REPO}/releases/download/{TAG}"


def dl(filename: str) -> str:
    """The download URL of one of the release's assets."""
    return f"{DL}/{filename}"

ZIPS = {
    "hfdr-phantom": b"PK hfdr phantom bytes " * 100,
    "srs-patient": b"PK srs patient bytes " * 300,
    "ffs-phantom": b"PK ffs phantom bytes " * 50,
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def asset(name: str, data: bytes, *, digest: bool = True, label: str = "") -> dict:
    entry = {
        "name": f"{name}.zip",
        "size": len(data),
        "browser_download_url": dl(f"{name}.zip"),
        "label": label,
    }
    if digest:
        entry["digest"] = f"sha256:{sha(data)}"
    return entry


def release(assets: list[dict] | None = None) -> dict:
    if assets is None:
        assets = [asset(name, data, label=f"The {name} pack") for name, data in ZIPS.items()]
        assets.append({"name": "notes.txt", "size": 3, "browser_download_url": dl("notes.txt"),
                       "label": "", "digest": "sha256:" + sha(b"abc")})
    return {"tag_name": TAG, "assets": assets}


class Response(io.BytesIO):
    """What urlopen returns, enough for the script: read() and a context manager."""


class FakeGitHub:
    """An opener serving one release and its downloads, recording every request."""

    def __init__(self, release_json: dict | None = None, files: dict[str, bytes] | None = None,
                 errors: dict[str, Exception] | None = None):
        self.release_json = release() if release_json is None else release_json
        self.files = {dl(f"{name}.zip"): data for name, data in ZIPS.items()} if files is None else files
        self.errors = errors or {}
        self.requests = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        url = request.full_url
        if url in self.errors:
            raise self.errors[url]
        if url == API_URL:
            return Response(json.dumps(self.release_json).encode("utf-8"))
        if url in self.files:
            return Response(self.files[url])
        raise urllib.error.HTTPError(url, 404, "Not Found", Message(), None)

    def downloads(self) -> list[str]:
        return [r.full_url.rsplit("/", 1)[1] for r in self.requests if r.full_url.startswith(DL)]


def http_error(url: str, code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError(url, code, "error", message, None)


@pytest.fixture(autouse=True)
def no_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)


def run(args, github, tmp_path) -> SimpleNamespace:
    """Run the script; the result has its exit `code`, `out` and `err` text, and `dest`."""
    out, err = io.StringIO(), io.StringIO()
    dest = tmp_path / "packs"
    code = get_packs.main([*args, "--dest", str(dest)], opener=github, out=out, err=err)
    return SimpleNamespace(code=code, out=out.getvalue(), err=err.getvalue(), dest=dest)


# --- what gets downloaded -------------------------------------------------------------------


def test_downloads_only_the_named_packs(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG, "srs-patient", "hfdr-phantom"], github, tmp_path)
    assert r.code == 0, r.err
    assert sorted(p.name for p in r.dest.iterdir()) == ["hfdr-phantom.zip", "srs-patient.zip"]
    assert (r.dest / "srs-patient.zip").read_bytes() == ZIPS["srs-patient"]
    assert github.downloads() == ["srs-patient.zip", "hfdr-phantom.zip"]  # the order asked for
    assert "2 of 2 pack(s)" in r.out
    assert "maprt-emulator --packs-dir" in r.out


def test_all_downloads_every_zip_and_ignores_other_assets(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG, "--all"], github, tmp_path)
    assert r.code == 0, r.err
    assert sorted(p.name for p in r.dest.iterdir()) == sorted(f"{name}.zip" for name in ZIPS)
    assert "notes.txt" not in github.downloads()


def test_no_names_and_no_all_lists_and_downloads_nothing(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG], github, tmp_path)
    assert r.code == 0
    assert github.downloads() == []
    assert not r.dest.exists()
    for name in ZIPS:
        assert name in r.out
    assert "Nothing downloaded" in r.out
    assert "--all" in r.out


def test_list_prints_names_sizes_and_labels_and_downloads_nothing(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG, "--list"], github, tmp_path)
    assert r.code == 0
    assert github.downloads() == []
    assert not r.dest.exists()
    assert "The srs-patient pack" in r.out
    assert get_packs.megabytes(len(ZIPS["srs-patient"])) in r.out
    assert "3 pack(s)" in r.out
    assert "notes" not in r.out
    assert "Nothing downloaded" not in r.out
    # sorted by name
    assert r.out.index("ffs-phantom") < r.out.index("hfdr-phantom") < r.out.index("srs-patient")


def test_a_name_may_end_in_zip_and_repeats_are_downloaded_once(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG, "SRS-patient.zip", "srs-patient"], github, tmp_path)
    assert r.code == 0, r.err
    assert github.downloads() == ["srs-patient.zip"]


def test_an_unknown_name_fails_with_the_available_names_and_downloads_nothing(tmp_path):
    github = FakeGitHub()
    r = run(["--release", TAG, "srs-patient", "no-such-pack"], github, tmp_path)
    assert r.code == 2
    assert "no-such-pack" in r.err
    for name in ZIPS:
        assert name in r.err
    assert github.downloads() == []


def test_dest_is_created(tmp_path):
    github = FakeGitHub()
    dest = tmp_path / "deep" / "folder"
    code = get_packs.main(["--release", TAG, "ffs-phantom", "--dest", str(dest)], opener=github,
                          out=io.StringIO(), err=io.StringIO())
    assert code == 0
    assert (dest / "ffs-phantom.zip").read_bytes() == ZIPS["ffs-phantom"]


# --- checking downloads ---------------------------------------------------------------------


def test_a_pack_already_present_with_the_right_sha256_is_skipped(tmp_path):
    dest = tmp_path / "packs"
    dest.mkdir()
    (dest / "srs-patient.zip").write_bytes(ZIPS["srs-patient"])
    github = FakeGitHub()
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 0
    assert github.downloads() == []
    assert "already present" in r.out


def test_a_pack_present_with_other_content_is_downloaded_again(tmp_path):
    dest = tmp_path / "packs"
    dest.mkdir()
    (dest / "srs-patient.zip").write_bytes(b"an old or damaged copy")
    github = FakeGitHub()
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 0, r.err
    assert github.downloads() == ["srs-patient.zip"]
    assert (r.dest / "srs-patient.zip").read_bytes() == ZIPS["srs-patient"]


def test_a_sha256_mismatch_fails_and_leaves_no_file(tmp_path):
    files = {dl(f"{name}.zip"): data for name, data in ZIPS.items()}
    files[dl("srs-patient.zip")] = b"X" * len(ZIPS["srs-patient"])  # right size, wrong bytes
    github = FakeGitHub(files=files)
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 1
    assert "sha256" in r.err
    assert list(r.dest.iterdir()) == []


def test_a_short_download_fails_and_leaves_no_file(tmp_path):
    files = {dl(f"{name}.zip"): data for name, data in ZIPS.items()}
    files[dl("srs-patient.zip")] = ZIPS["srs-patient"][:100]
    github = FakeGitHub(files=files)
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 1
    assert "expected" in r.err
    assert list(r.dest.iterdir()) == []


def test_a_failed_download_still_downloads_the_others_and_exits_1(tmp_path):
    github = FakeGitHub(errors={dl("hfdr-phantom.zip"): urllib.error.URLError("connection reset")})
    r = run(["--release", TAG, "hfdr-phantom", "srs-patient"], github, tmp_path)
    assert r.code == 1
    assert "connection reset" in r.err
    assert sorted(p.name for p in r.dest.iterdir()) == ["srs-patient.zip"]
    assert "1 of 2 pack(s)" in r.out


def test_no_digest_warns_and_downloads_unchecked(tmp_path):
    rel = release([asset("srs-patient", ZIPS["srs-patient"], digest=False)])
    github = FakeGitHub(release_json=rel)
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 0
    assert "no sha256" in r.err
    assert (r.dest / "srs-patient.zip").read_bytes() == ZIPS["srs-patient"]


def test_a_label_equal_to_the_file_name_is_not_shown_as_a_description():
    rel = release([asset("srs-patient", ZIPS["srs-patient"], label="srs-patient.zip")])
    packs = get_packs.fetch_release(REPO, TAG, opener=FakeGitHub(release_json=rel))
    assert packs[0].label == ""


# --- talking to GitHub ----------------------------------------------------------------------


def test_the_release_is_asked_for_by_tag(tmp_path):
    github = FakeGitHub()
    run(["--release", TAG, "--list"], github, tmp_path)
    assert github.requests[0].full_url == API_URL
    assert github.requests[0].get_header("User-agent") == get_packs.USER_AGENT


def test_a_token_goes_to_the_api_only(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    github = FakeGitHub()
    r = run(["--release", TAG, "srs-patient"], github, tmp_path)
    assert r.code == 0
    api, download = github.requests
    assert api.get_header("Authorization") == "Bearer t0ken"
    assert download.get_header("Authorization") is None


def test_no_token_sends_no_authorization(tmp_path):
    github = FakeGitHub()
    run(["--release", TAG, "--list"], github, tmp_path)
    assert github.requests[0].get_header("Authorization") is None


def test_an_unknown_release_says_so(tmp_path):
    github = FakeGitHub(errors={API_URL: http_error(API_URL, 404)})
    r = run(["--release", TAG, "--all"], github, tmp_path)
    assert r.code == 1
    assert "No release 'packs-v1'" in r.err
    assert not r.dest.exists()


def test_the_rate_limit_says_how_to_raise_it(tmp_path):
    error = http_error(API_URL, 403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1900000000"})
    github = FakeGitHub(errors={API_URL: error})
    r = run(["--release", TAG, "--list"], github, tmp_path)
    assert r.code == 1
    assert "GITHUB_TOKEN" in r.err
    assert "resets at" in r.err


def test_no_network_says_so(tmp_path):
    github = FakeGitHub(errors={API_URL: urllib.error.URLError("nodename nor servname provided")})
    r = run(["--release", TAG, "--list"], github, tmp_path)
    assert r.code == 1
    assert "Could not reach GitHub" in r.err


def test_a_release_without_zips_fails(tmp_path):
    github = FakeGitHub(release_json={"tag_name": TAG, "assets": []})
    r = run(["--release", TAG, "--all"], github, tmp_path)
    assert r.code == 1
    assert "has no packs" in r.err


def test_another_repo_can_be_named(tmp_path):
    github = FakeGitHub(errors={})
    url = f"https://api.github.com/repos/someone/fork/releases/tags/{TAG}"
    github.errors[url] = http_error(url, 404)
    r = run(["--release", TAG, "--repo", "someone/fork", "--list"], github, tmp_path)
    assert github.requests[0].full_url == url
    assert "someone/fork" in r.err


# --- usage errors ---------------------------------------------------------------------------


@pytest.mark.parametrize("args", [
    [],  # --release is required
    ["--release", "latest", "--all"],
    ["--release", "Latest", "srs-patient"],
    ["--release", TAG, "--all", "srs-patient"],
    ["--release", TAG, "--list", "srs-patient"],
    ["--release", TAG, "--all", "--list"],
])
def test_usage_errors_exit_2_without_calling_github(args, tmp_path, capsys):
    github = FakeGitHub()
    with pytest.raises(SystemExit) as exc:
        get_packs.main([*args, "--dest", str(tmp_path / "packs")], opener=github,
                       out=io.StringIO(), err=io.StringIO())
    assert exc.value.code == 2
    assert github.requests == []
