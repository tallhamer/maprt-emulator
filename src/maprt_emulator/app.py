"""The FastAPI application: wiring, the ``Server`` header, and envelope error rendering.

It serves the seven MapRT REST routes, plus FastAPI's own pages (``/docs``, ``/redoc``,
``/openapi.json``), which every FastAPI application has, and the inventory page (``/inventory``,
``/inventory.md``: what is served and what a call can reach; not in the schema). The pages describe only what an
appliance takes and sends (:mod:`maprt_emulator.replies`), so a call tried there is a real one. There are no emulator control routes: a
real appliance has none, and a client tested here must not come to rely on one. A real appliance
publishes no API pages either, so a client must not depend on those. Faults are chosen when the
emulator starts (``--scenario``), not over HTTP.

Two things to remember when reading responses:

* Every endpoint but ``GetMap`` answers with the envelope: ``{"data", "errorData", "success",
  "errorCode", "errorCodeType"}`` on success, and the same without ``data`` on a negative result.
* **A ``200`` status does not mean the call succeeded.** A negative result — bad token, unlicensed
  feature, ambiguous room name — comes back at ``200`` with ``success: false``.
"""

import asyncio
import logging

from fastapi import FastAPI, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from maprt_emulator import envelope
from maprt_emulator.auth import bearer_scheme
from maprt_emulator.library import load_library, serves_packs
from maprt_emulator.maps import MapLibrary
from maprt_emulator.routes import beams, getmap, inventory, ping, rooms, surfaces
from maprt_emulator.routes.getmap import MapCache
from maprt_emulator.scenarios import ScenarioState
from maprt_emulator.settings import API_VERSION, SERVER_VERSION, EmulatorSettings
from maprt_emulator.store import Catalog

logger = logging.getLogger("maprt_emulator")

#: ``create_app``'s default for ``library``: load the packs the settings ask for.
_FROM_SETTINGS = object()


def create_app(settings=None, library=_FROM_SETTINGS):
    """Build the application. ``settings`` defaults to one resolved from the environment.

    ``library`` is the :class:`maprt_emulator.library.PackLibrary` to serve (or ``None`` for none),
    when the caller has already loaded it, as the command line does to report it in the banner;
    by default the packs the settings ask for are loaded here.
    """
    settings = settings or EmulatorSettings()

    # FastAPI's default /docs, /redoc and /openapi.json stay: they come with any FastAPI app.
    app = FastAPI(title="MapRT Emulator", version="2.0.0", description=DOCS_DESCRIPTION)
    _describe_only_the_api(app)

    app.state.settings = settings
    app.state.token = settings.resolve_token()
    app.state.catalog = Catalog.load(settings.data_dir)
    app.state.library = load_library(settings) if library is _FROM_SETTINGS else library
    # Decision 3: with packs loaded, the emulator presents the newest appliance they were recorded
    # on, in the Server header and the ping reply. Nothing else changes with it.
    appliance = app.state.library.appliance if serves_packs(app) else None
    app.state.server_version = appliance["server"] if appliance else SERVER_VERSION
    app.state.api_version = appliance["apiVersion"] if appliance else API_VERSION
    app.state.maps = MapLibrary.load(settings.data_dir)
    app.state.map_cache = MapCache(app.state.catalog, library=app.state.library)
    app.state.scenarios = ScenarioState(settings)

    @app.exception_handler(envelope.MapRTFailure)
    async def _render_failure(_request, exc: envelope.MapRTFailure):
        """Render a reported failure as the MapRT envelope at the status the raiser chose.

        Note the status is usually **200**: that is the API's own convention for a negative
        result, not an oversight here.
        """
        if exc.detail:
            logger.warning("%s: %s", exc.error_code, exc.detail)
        return JSONResponse(exc.envelope(), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _render_validation_error(_request, exc: RequestValidationError):
        """Render a malformed request body as the MapRT envelope at HTTP 400.

        FastAPI's own answer would be ``422`` with ``{"detail": [...]}``, which is a perfectly
        good REST convention and the wrong one here twice over: the spec (§5.3) puts a rejected
        request at a 4xx such as 400, not 422, and the body is not the
        envelope, so a MapRT client that has one parser for every response cannot read it.

        The validation detail goes to the server log, not ``errorData``: no recorded error
        carries anything there, so a client must not learn to read it.
        """
        logger.warning("InvalidArgument: %s", "; ".join(_readable(error) for error in exc.errors()))
        return JSONResponse(envelope.fail(*envelope.INVALID_ARGUMENT), status_code=400)

    @app.middleware("http")
    async def _appliance_behaviour(request, call_next):
        """Latency, the armed scenario's transport-level faults, and the ``Server`` header.

        The faults are applied only to ``/integration`` paths, which is every route there is; an
        unknown path still gets a plain 404.
        """
        scenarios_state = app.state.scenarios
        is_api = request.url.path.startswith("/integration")

        if settings.latency_ms:
            await asyncio.sleep(settings.latency_ms / 1000)

        if is_api and scenarios_state.extra_delay:
            await asyncio.sleep(scenarios_state.extra_delay)

        if is_api and scenarios_state.take_throttle_slot():
            # The spec (§2.2.1.3) allows a server to throttle: to tell a client it can't take the
            # request now and that it should try again. Retry-After tells a client how long to
            # wait instead of hammering.
            logger.warning("ServerBusy: the 'throttle' scenario refused %s", request.url.path)
            response = JSONResponse(envelope.fail(*envelope.SERVER_BUSY), status_code=503)
            response.headers["Retry-After"] = "1"
            response.headers["server"] = app.state.server_version
            return response

        if is_api and scenarios_state.forced_status:
            # A 5xx has no envelope to read on a real appliance, and a client that assumes it can
            # always parse JSON fails inside its own error handler. One is sent anyway here, so
            # the status is the thing being exercised rather than the body.
            logger.warning("InternalError: the 'server-error' scenario answered %s", request.url.path)
            response = JSONResponse(envelope.fail(*envelope.INTERNAL_ERROR),
                                    status_code=scenarios_state.forced_status)
            response.headers["server"] = app.state.server_version
            return response

        response = await call_next(request)
        # Every appliance response carries a Server header, Saturn/<version> (spec §5.1). Worth
        # reproducing — it is how a client can tell what it is talking to,
        # and a notebook reads it to confirm the connection landed where it thinks.
        response.headers["server"] = app.state.server_version
        return response

    # Security() here only documents the Bearer token for /docs; require_token does the checking.
    for router in (ping.router, rooms.router, surfaces.router, getmap.router, beams.router):
        app.include_router(router, dependencies=[Security(bearer_scheme)])
    # A page for people, like /docs: no token, and not part of the API (left out of the schema).
    app.include_router(inventory.router)

    return app


DOCS_DESCRIPTION = """\
A local emulator of the MapRT API 2.0 REST routes. Teaching and testing use only. A real
appliance publishes no page like this one.

To try a call: **Authorize** with the token the emulator printed at start. A browser can't send
the `VisionRT.Integration.Saturn/<version>` User-Agent an appliance requires, so start the
emulator with `--lenient-user-agent`, or every call here answers `VersionMismatch`.

[The inventory](/inventory) ([Markdown](/inventory.md)) lists what this emulator serves: with
packs loaded, every recorded request, with a body to copy that gets the recorded reply.
"""


def _describe_only_the_api(app):
    """Leave FastAPI's 422 validation reply out of the schema: the emulator never sends one.

    A malformed body gets the MapRT error envelope at 400 (``_render_validation_error``), and
    each POST route documents that 400 itself.
    """
    generate = app.openapi

    def openapi():
        schema = generate()
        for operations in schema.get("paths", {}).values():
            for operation in operations.values():
                operation.get("responses", {}).pop("422", None)
        for name in ("HTTPValidationError", "ValidationError"):
            schema.get("components", {}).get("schemas", {}).pop(name, None)
        return schema

    app.openapi = openapi


def _readable(error):
    """One pydantic validation error as a short string, without its internal bookkeeping."""
    location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
    return f"{location or 'body'}: {error.get('msg', 'invalid')}"
