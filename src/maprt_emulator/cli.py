"""``maprt-emulator`` — starting the emulator, and the token banner.

The emulator mints a **fresh token every time it starts** and makes it available two ways:

* printed in a banner, for someone watching a terminal;
* written to ``.maprt_emulator/token``, for everyone else.

The file is not a convenience, it is the only thing that works on Binder and Colab, where the
server runs as a background process and nobody ever sees its output.
"""

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from maprt_emulator.app import create_app
from maprt_emulator.library import load_library
from maprt_emulator.maps import MapLibrary, MapLibraryError
from maprt_emulator.scenarios import SCENARIOS, catalogue
from maprt_emulator.settings import LICENSABLE, EmulatorSettings
from maprt_emulator.store import Catalog, CatalogError

BANNER_WIDTH = 74


def _stdout_can_print_the_banner():
    """Switch stdout to UTF-8 if its encoding can't hold the banner's box and dash characters.

    On Windows a redirected or piped stdout (a launcher capturing the output, a log file) is
    cp1252, and printing the box there raised ``UnicodeEncodeError`` before the server started.
    """
    try:
        "╭─╮│╰╯—".encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _box(lines):
    inner = BANNER_WIDTH - 2
    out = ["╭" + "─" * inner + "╮"]
    for line in lines:
        out.append("│" + line.ljust(inner)[:inner] + "│")
    out.append("╰" + "─" * inner + "╯")
    return "\n".join(out)


def _elide(path, width):
    """Shorten a path to ``width``, dropping leading components rather than trailing ones.

    Truncating from the right hides the part that matters — *which* data directory this is — and
    cuts mid-word, which reads like a bug in the banner rather than a deliberate shortening.
    """
    text = str(path)
    if len(text) <= width:
        return text
    parts = text.split("/")
    for start in range(1, len(parts)):
        candidate = ".../" + "/".join(parts[start:])
        if len(candidate) <= width:
            return candidate
    # A single component longer than the whole field: nothing to drop, so cut the front.
    return "..." + text[-(width - 3):]


def banner(settings, catalog, token_path, maps=None, library=None):
    """The startup box, plus the copy-paste line for a notebook, and what the packs need said."""
    serving_packs = library is not None and bool(library.packs)
    warnings = []
    if settings.scenario != "ok":
        warnings.append(f"  Scenario    {settings.scenario}: {SCENARIOS[settings.scenario].description}")
    if settings.map_source != "auto":
        warnings.append(f"  Maps        --map-source {settings.map_source}")
    if settings.shuffle_map:
        warnings.append("  Maps        triples served in random order")
    if settings.unlicensed:
        warnings.append(f"  Unlicensed  {', '.join(settings.unlicensed)}")
    if settings.auth_401:
        warnings.append("  Auth        a token problem returns HTTP 401 (an appliance sends 200)")
    if not settings.strict_user_agent:
        warnings.append("  User-Agent  not enforced (an appliance answers VersionMismatch)")
    if settings.open_ping:
        warnings.append("  Ping        served WITHOUT a token")
    if settings.source_headers:
        warnings.append("  Headers     X-Emulator-* source headers sent (an appliance sends none)")

    lines = [
        "  MapRT Emulator  —  NOT a medical device. Teaching only.",
        "",
        f"  Base URL    {settings.base_url}",
        f"  Docs        {settings.base_url}/docs",
        *(["              (Try it out needs --lenient-user-agent)"] if settings.strict_user_agent else []),
        *_data_lines(settings, catalog, maps, library),
        f"  Token file  {token_path}",
    ]
    if serving_packs and library.warnings:
        warnings.append("  Appliance   packs from several versions; see below")
    if warnings:
        lines.extend(["", *warnings])

    return "\n".join([
        "",
        _box(lines),
        *_pack_report(library),
        "",
        "Copy this line into your notebook (or let it read the token file):",
        "",
        f"    MAPRT_EMULATOR_TOKEN={settings.resolve_token()}",
        "",
        "Press Ctrl+C to stop the server.",
        "",
    ])


def _data_lines(settings, catalog, maps, library):
    """The banner's lines about what is served: the packs, or the catalog."""
    width = BANNER_WIDTH - 16
    if library is not None and library.packs:
        appliance = library.appliance
        first, *rest = _wrap(library.summary(), BANNER_WIDTH - 16)
        return [f"  Packs       {first}", *(f"              {part}" for part in rest),
                f"              {_elide(settings.packs_dir, width)}",
                f"  Appliance   {appliance['server']} (API {appliance['apiVersion']})"]
    lines = [f"  Data        {catalog.summary()}" + (f" · {maps.summary()}" if maps else ""),
             f"              {_elide(settings.data_dir, width)}"]
    if settings.no_packs:
        lines.append("  Packs       off (--no-packs)")
    elif library is not None:
        lines.append(f"  Packs       none in {_elide(settings.packs_dir, width - 8)}")
    return lines


def _wrap(text, width):
    """``text`` as lines of at most ``width``, broken after its ``;`` and ``,`` separators."""
    lines = []
    for clause in text.split("; "):
        line = ""
        for part in clause.split(", "):
            candidate = f"{line}, {part}" if line else part
            if line and len(candidate) + 1 > width:
                lines.append(line + ",")
                line = part
            else:
                line = candidate
        lines.append(line)
    return lines


def _pack_report(library):
    """Below the box: each pack warning and notice once, and every skipped pack with its findings."""
    if library is None or not (library.warnings or library.notices or library.skipped):
        return []
    lines = ["", "Packs:"]
    lines += [f"  ! {warning}" for warning in library.warnings]
    for entry in library.skipped:
        lines.append(f"  ! Skipped {entry.name or entry.path} ({entry.path}), which failed its checks:")
        lines += [f"      - {finding}" for finding in entry.findings]
    lines += [f"  · {notice}" for notice in library.notices]
    return lines


def _parser(prog="maprt-emulator"):
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Run a local emulator of the Vision RT MapRT API 2.0. Teaching use only.",
        epilog="maprt-emulator inventory [the same options] prints, as Markdown, what those options "
               "would serve, without starting a server (also at /inventory and /inventory.md).",
    )
    parser.add_argument("--host", default=None, help="interface to bind (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port to bind (default 8000)")
    parser.add_argument("--token", default=None,
                        help="use this API token instead of generating a fresh one")
    parser.add_argument("--data-dir", default=None,
                        help="the catalog to serve (default: the nearest data/MapRT/ directory)")
    parser.add_argument("--packs-dir", default=None,
                        help="serve the session packs in this folder (every .zip, and every folder "
                             "with a manifest.json); they replace the catalog")
    parser.add_argument("--pack", action="append", default=None, metavar="NAME",
                        help="serve only this pack from --packs-dir, by its manifest name "
                             "(repeatable); a named pack that fails its checks refuses startup")
    parser.add_argument("--no-packs", action="store_true",
                        help="read no pack, even with MAPRT_EMULATOR_PACKS_DIR set: serve the catalog")
    parser.add_argument("--state-dir", default=None,
                        help="where to write the token file (default .maprt_emulator)")
    parser.add_argument("--unlicensed", default=None,
                        help=f"comma-separated features to report as unlicensed: "
                             f"{', '.join(LICENSABLE)}")
    parser.add_argument("--scenario", default=None, choices=sorted(SCENARIOS),
                        help="arm a fault for the whole run (default ok: none); see --list-scenarios")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="describe every scenario and exit")
    parser.add_argument("--latency-ms", type=int, default=None,
                        help="artificial delay per request, so calls are visibly not free")
    parser.add_argument("--map-source", default=None, choices=("auto", "recorded", "synthetic"),
                        help="auto prefers a recorded map; recorded refuses to invent one; "
                             "synthetic ignores the recordings (default auto)")
    parser.add_argument("--seed", type=int, default=None,
                        help="varies the clearance model slightly; same seed, same maps")
    parser.add_argument("--map-seconds", type=float, default=None,
                        help="roughly how long GetMap takes to stream (default 3; a real "
                             "appliance takes minutes)")
    parser.add_argument("--shuffle-map", action="store_true",
                        help="randomise the order of the map's triples; the spec says the list "
                             "is unordered, so a client must not depend on it")
    parser.add_argument("--auth-401", action="store_true",
                        help="answer a token problem with HTTP 401 instead of the appliance's 200")
    parser.add_argument("--lenient-user-agent", action="store_true",
                        help="accept any User-Agent (only log it); an appliance answers one that "
                             "isn't VisionRT.Integration.Saturn/<2.0 or older> with VersionMismatch")
    parser.add_argument("--open-ping", action="store_true",
                        help="serve /integration/ping without a token (debugging aid)")
    parser.add_argument("--source-headers", action="store_true",
                        help="name the answering source (recorded or synthetic) in X-Emulator-* "
                             "response headers; an appliance sends none, so they are off by default")
    parser.add_argument("--check-data", action="store_true",
                        help="validate the data directory (and the packs, if any) and exit, "
                             "without starting a server")
    parser.add_argument("--reload", action="store_true", help="reload on code changes (developing)")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    return parser


def settings_from_args(args):
    """Turn parsed arguments into :class:`EmulatorSettings`, leaving unset flags to the environment."""
    overrides = {}
    if args.host is not None:
        overrides["host"] = args.host
    if args.port is not None:
        overrides["port"] = args.port
    if args.token is not None:
        overrides["token"] = args.token
    if args.data_dir is not None:
        overrides["data_dir"] = Path(args.data_dir)
    if args.state_dir is not None:
        overrides["state_dir"] = Path(args.state_dir)
    if args.no_packs and (args.packs_dir is not None or args.pack):
        raise SystemExit("maprt-emulator: --no-packs reads no pack, so it can't be given with "
                         "--packs-dir or --pack.")
    if args.packs_dir is not None:
        overrides["packs_dir"] = Path(args.packs_dir)
    if args.pack:
        overrides["packs"] = tuple(args.pack)
    if args.latency_ms is not None:
        overrides["latency_ms"] = args.latency_ms
    if args.map_source is not None:
        overrides["map_source"] = args.map_source
    if args.seed is not None:
        overrides["seed"] = args.seed
    if args.scenario is not None:
        overrides["scenario"] = args.scenario
    if args.map_seconds is not None:
        overrides["map_seconds"] = args.map_seconds
    if args.unlicensed is not None:
        features = tuple(f.strip() for f in args.unlicensed.split(",") if f.strip())
        unknown = [f for f in features if f not in LICENSABLE]
        if unknown:
            raise SystemExit(f"maprt-emulator: unknown feature(s) {', '.join(unknown)}. "
                             f"Choose from: {', '.join(LICENSABLE)}")
        overrides["unlicensed"] = features
    # Store-true flags: only pass them when set, so the environment can still turn them on.
    for flag in ("auth_401", "open_ping", "shuffle_map", "source_headers", "no_packs"):
        if getattr(args, flag):
            overrides[flag] = True
    if args.lenient_user_agent:
        overrides["strict_user_agent"] = False
    return EmulatorSettings(**overrides)


def list_scenarios():
    """Every scenario: what it does, and what it is there to teach."""
    lines = []
    for scenario in catalogue():
        lines += [scenario["name"], f"    {scenario['description']}"]
        if scenario["teaches"]:
            lines.append(f"    Teaches: {scenario['teaches']}")
        lines.append("")
    return "\n".join(lines)


def inventory(argv):
    """``maprt-emulator inventory [--packs-dir …] [--pack …] [--no-packs] [--data-dir …]``: print
    the inventory page's Markdown for what these settings would serve, without starting a server."""
    settings = settings_from_args(_parser("maprt-emulator inventory").parse_args(argv))
    from maprt_emulator import inventory as page

    try:
        app = create_app(settings)
    except CatalogError as exc:
        raise SystemExit(f"maprt-emulator: {exc}") from exc
    _stdout_can_print_the_banner()
    print(page.markdown(page.build(app)), end="")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["inventory"]:
        inventory(argv[1:])
        return
    args = _parser().parse_args(argv)
    if args.list_scenarios:
        print(list_scenarios())
        return
    settings = settings_from_args(args)

    if args.check_data:
        # Before the catalog load below, because that one raises on the first problem it meets.
        # The checker reports everything at once, which is what you want while editing data.
        from maprt_emulator.check import main as check_main
        raise SystemExit(check_main(settings.data_dir, settings))

    # Load the catalog here, before uvicorn starts, so a data problem is a clear message at the
    # prompt rather than a traceback inside a worker during someone's first notebook cell.
    try:
        catalog = Catalog.load(settings.data_dir)
    except CatalogError as exc:
        raise SystemExit(f"maprt-emulator: {exc}") from exc

    try:
        maps = MapLibrary.load(settings.data_dir)
    except MapLibraryError as exc:
        raise SystemExit(f"maprt-emulator: {exc}") from exc

    # The packs too, once, here: a refusal is a message at the prompt, the banner reports what was
    # loaded, and the app is handed the same library rather than loading it again.
    try:
        library = load_library(settings)
    except CatalogError as exc:
        raise SystemExit(f"maprt-emulator: {exc}") from exc

    packs_report = {"off": True} if settings.no_packs else (library.report() if library is not None else None)
    token_path = settings.write_token(packs=packs_report)
    _stdout_can_print_the_banner()
    print(banner(settings, catalog, token_path, maps, library), flush=True)

    # Build the app HERE, from the settings we just parsed and just described in the banner, and
    # hand uvicorn the object. The alternative — giving uvicorn an import string and letting it
    # call create_app() itself — re-resolves EmulatorSettings from the environment in the server
    # process, so every command-line flag is silently dropped while the banner still claims it
    # is in effect. That is worse than not having the flags.
    #
    # --reload is the one case that cannot work this way: reloading means re-importing in a child
    # process, which needs an import string. There, the settings go through the environment
    # instead, which is why they all have to be environment-expressible.
    if args.reload:
        os.environ.update(settings.as_env())
        target, factory = "maprt_emulator.app:create_app", True
    else:
        target, factory = create_app(settings, library), False

    uvicorn.run(
        target,
        factory=factory,
        host=settings.host,
        port=settings.port,
        reload=args.reload,
        log_level=args.log_level,
        # uvicorn would otherwise advertise itself in the Server header; the middleware replaces
        # it with Saturn/... to match a real appliance.
        server_header=False,
    )


def address(argv, prog="maprt-emulator"):
    """The ``(host, port)`` the emulator would listen on, given these arguments.

    What a launcher (the SGRT Scripting Workshop's ``maprt-workshop``) calls before starting
    the emulator, so it knows what to wait for. Parsing here also means ``maprt-workshop --help``
    and a mistyped flag are reported at the prompt, before anything starts.
    """
    settings = settings_from_args(_parser(prog).parse_args(argv))
    return settings.host, settings.port


if __name__ == "__main__":
    main()
