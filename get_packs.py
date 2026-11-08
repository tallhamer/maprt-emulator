#!/usr/bin/env python3
"""Download MapRT Emulator session packs from one of this repository's releases.

    python get_packs.py --release packs-v1 hfdr-phantom srs-patient   # just these packs
    python get_packs.py --release packs-v1 --all                      # every pack in the release
    python get_packs.py --release packs-v1 --list                     # list them, download nothing

Only the packs you name are downloaded, or every one with --all. With neither, the release's packs
are listed and nothing is downloaded. The zips go into ./packs (--dest to change it), ready for
``maprt-emulator --packs-dir packs``.

The release tag is required: a course or a notebook should always get the same packs, so "latest"
is not accepted. Each download is checked against the sha256 GitHub publishes for it, and a pack
already in the folder with the right sha256 is not downloaded again.

Standard library only (Python 3.11+), so it runs anywhere Python does with nothing installed. It
makes one call to GitHub's public API per run; set GITHUB_TOKEN (or GH_TOKEN) if you hit GitHub's
limit for anonymous calls. The token is sent to the API only, never with the downloads.

Exit status: 0 when everything asked for was downloaded or already present, 1 when anything
failed, 2 for a usage error (including a pack name the release doesn't have).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

REPO = "tallhamer/maprt-emulator"
API_ROOT = "https://api.github.com"
USER_AGENT = "maprt-emulator-get-packs"
API_TIMEOUT = 30  # seconds
DOWNLOAD_TIMEOUT = 60  # seconds without a byte, not for the whole download
CHUNK = 1 << 20

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

Opener = Callable[..., object]


class PackError(Exception):
    """A failure to report to the user: one line, no traceback."""


@dataclass(frozen=True)
class Pack:
    """One pack zip in a release."""

    name: str  # the pack's name: the zip's name without ".zip"
    filename: str
    size: int
    url: str
    sha256: str | None  # from the asset's digest; None if GitHub gave none
    label: str  # the asset's label (a short description), or ""


def release_url(repo: str, tag: str) -> str:
    return f"{API_ROOT}/repos/{repo}/releases/tags/{urllib.parse.quote(tag, safe='')}"


def fetch_release(repo: str, tag: str, *, token: str | None = None,
                  opener: Opener = urllib.request.urlopen) -> list[Pack]:
    """The release's pack zips (its `.zip` assets), sorted by name."""
    url = release_url(repo, tag)
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=API_TIMEOUT) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise PackError(_http_error_message(exc, repo, tag, url)) from None
    except urllib.error.URLError as exc:
        raise PackError(f"Could not reach GitHub ({exc.reason}). Check the network connection.") from None
    except (TimeoutError, OSError) as exc:
        raise PackError(f"Could not reach GitHub ({exc}). Check the network connection.") from None
    except ValueError:
        raise PackError(f"GitHub's reply for release {tag!r} was not valid JSON.") from None

    packs = []
    for asset in release.get("assets") or []:
        filename = asset.get("name") or ""
        if not filename.lower().endswith(".zip"):
            continue
        digest = asset.get("digest") or ""
        sha256 = digest.split(":", 1)[1].lower() if digest.lower().startswith("sha256:") else None
        label = (asset.get("label") or "").strip()
        packs.append(Pack(
            name=filename[:-len(".zip")],
            filename=filename,
            size=int(asset.get("size") or 0),
            url=asset["browser_download_url"],
            sha256=sha256,
            label="" if label == filename else label,
        ))
    return sorted(packs, key=lambda pack: pack.name.lower())


def _http_error_message(exc: urllib.error.HTTPError, repo: str, tag: str, url: str) -> str:
    if exc.code == 404:
        return (f"No release {tag!r} in {repo}. Check the tag on "
                f"https://github.com/{repo}/releases (a draft release is not public).")
    headers = exc.headers or {}
    if exc.code in (403, 429) and (headers.get("x-ratelimit-remaining") == "0" or exc.code == 429):
        reset = headers.get("x-ratelimit-reset")
        when = ""
        if reset and reset.isdigit():
            when = f" It resets at {time.strftime('%H:%M', time.localtime(int(reset)))}."
        return ("GitHub's limit for anonymous API calls was reached." + when +
                " Set GITHUB_TOKEN (or GH_TOKEN) to a GitHub token to raise it.")
    return f"GitHub answered HTTP {exc.code} for {url}."


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(pack: Pack, dest: Path, *, opener: Opener = urllib.request.urlopen) -> str:
    """Put `pack` in `dest`. Returns "present" (already there, sha256 matches) or "downloaded".

    The zip is written to `<name>.zip.part` and renamed only once its size and sha256 are right,
    so an interrupted or bad download never looks like a pack.
    """
    target = dest / pack.filename
    if pack.sha256 and target.is_file() and sha256_of(target) == pack.sha256:
        return "present"

    part = dest / (pack.filename + ".part")
    digest = hashlib.sha256()
    received = 0
    request = urllib.request.Request(pack.url, headers={"User-Agent": USER_AGENT})
    try:
        with opener(request, timeout=DOWNLOAD_TIMEOUT) as resp, part.open("wb") as handle:
            for block in iter(lambda: resp.read(CHUNK), b""):
                digest.update(block)
                handle.write(block)
                received += len(block)
    except urllib.error.HTTPError as exc:
        _discard(part)
        raise PackError(f"{pack.filename}: GitHub answered HTTP {exc.code}.") from None
    except urllib.error.URLError as exc:
        _discard(part)
        raise PackError(f"{pack.filename}: download failed ({exc.reason}).") from None
    except OSError as exc:  # includes TimeoutError and a full disk
        _discard(part)
        raise PackError(f"{pack.filename}: download failed ({exc}).") from None

    if pack.size and received != pack.size:
        _discard(part)
        raise PackError(f"{pack.filename}: got {received} bytes, expected {pack.size}. Try again.")
    if pack.sha256 and digest.hexdigest() != pack.sha256:
        _discard(part)
        raise PackError(f"{pack.filename}: sha256 doesn't match the release's "
                        f"({digest.hexdigest()} != {pack.sha256}). Not saved; try again.")
    os.replace(part, target)
    return "downloaded"


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def megabytes(size: int) -> str:
    """Binary megabytes, as GitHub's release page and the release notes show them."""
    return f"{size / (1024 * 1024):.1f} MB"


def print_packs(packs: list[Pack], tag: str, repo: str, out: TextIO) -> None:
    width = max(len(pack.name) for pack in packs)
    print(f"Packs in release {tag} of {repo}:", file=out)
    print(file=out)
    for pack in packs:
        line = f"  {pack.name:<{width}}  {megabytes(pack.size):>9}"
        if pack.label:
            line += f"  {pack.label}"
        print(line, file=out)
    print(file=out)
    total = sum(pack.size for pack in packs)
    print(f"{len(packs)} pack(s), {megabytes(total)} in all.", file=out)


def choose(packs: list[Pack], names: list[str]) -> tuple[list[Pack], list[str]]:
    """The packs named (a trailing ".zip" is allowed), in the order given, and the unknown names."""
    by_name = {pack.name.lower(): pack for pack in packs}
    chosen: list[Pack] = []
    unknown: list[str] = []
    for name in names:
        key = name.lower().removesuffix(".zip")
        pack = by_name.get(key)
        if pack is None:
            unknown.append(name)
        elif pack not in chosen:
            chosen.append(pack)
    return chosen, unknown


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="get_packs.py",
        description="Download MapRT Emulator session packs from a release of "
                    f"github.com/{REPO}. Only the packs you name are downloaded, or every one "
                    "with --all; with neither, the release's packs are listed.",
        epilog="examples:\n"
               "  python get_packs.py --release packs-v1 --list\n"
               "  python get_packs.py --release packs-v1 hfdr-phantom srs-patient\n"
               "  python get_packs.py --release packs-v1 --all\n"
               "then: maprt-emulator --packs-dir packs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("packs", nargs="*", metavar="PACK", help="the packs to download, by name")
    parser.add_argument("--release", required=True, metavar="TAG",
                        help="the release to download from, e.g. packs-v1 (a tag, never 'latest')")
    which = parser.add_mutually_exclusive_group()
    which.add_argument("--all", action="store_true", help="download every pack in the release")
    which.add_argument("--list", action="store_true", help="list the release's packs and download nothing")
    parser.add_argument("--dest", type=Path, default=Path("packs"), metavar="FOLDER",
                        help="where to put the zips (default: ./packs)")
    parser.add_argument("--repo", default=REPO, metavar="OWNER/NAME",
                        help=f"the GitHub repository (default: {REPO})")
    return parser


def main(argv: list[str] | None = None, *, opener: Opener = urllib.request.urlopen,
         out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.packs and (args.all or args.list):
        parser.error("name the packs you want, or use --all or --list, not both")
    if args.release.strip().lower() == "latest":
        parser.error("give a release tag such as packs-v1; 'latest' is not accepted, so the packs "
                     "you get never change underneath you")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None
    try:
        packs = fetch_release(args.repo, args.release, token=token, opener=opener)
    except PackError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_FAILED
    if not packs:
        print(f"error: release {args.release} of {args.repo} has no packs (.zip assets).", file=err)
        return EXIT_FAILED

    if args.list or not (args.all or args.packs):
        print_packs(packs, args.release, args.repo, out)
        if not args.list:
            print(file=out)
            print("Nothing downloaded. Name the packs you want, for example:", file=out)
            print(f"  python get_packs.py --release {args.release} {packs[0].name}", file=out)
            print("or download every pack with --all.", file=out)
        return EXIT_OK

    if args.all:
        chosen = packs
    else:
        chosen, unknown = choose(packs, args.packs)
        if unknown:
            print(f"error: release {args.release} has no pack named {', '.join(unknown)}.", file=err)
            print(f"Its packs: {', '.join(pack.name for pack in packs)}", file=err)
            return EXIT_USAGE

    dest: Path = args.dest
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"error: can't create {dest}: {exc}", file=err)
        return EXIT_FAILED

    failed = 0
    for pack in chosen:
        if not pack.sha256:
            print(f"warning: GitHub gave no sha256 for {pack.filename}; it can't be checked.", file=err)
        print(f"{pack.filename} ({megabytes(pack.size)}) ... ", end="", file=out, flush=True)
        try:
            result = download(pack, dest, opener=opener)
        except PackError as exc:
            print("failed", file=out)
            print(f"error: {exc}", file=err)
            failed += 1
            continue
        print("already present" if result == "present" else "done", file=out)

    got = len(chosen) - failed
    print(f"{got} of {len(chosen)} pack(s) in {dest}.", file=out)
    if failed:
        return EXIT_FAILED
    print(f"Start the emulator on them with: maprt-emulator --packs-dir {dest}", file=out)
    return EXIT_OK


if __name__ == "__main__":
    # A Windows console may not encode a pack's label; never fail on it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(main())
