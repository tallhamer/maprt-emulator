"""Fixtures for the emulator tests: a built app, and clients with and without a valid token."""

import json

import pytest
from fastapi.testclient import TestClient

from maprt_emulator.app import create_app
from maprt_emulator.settings import BUILTIN_DATA_DIR, EmulatorSettings

TOKEN = "test-token-not-a-real-secret"

#: The catalog shipped inside the package — what the tests serve.
REPO_DATA = BUILTIN_DATA_DIR


@pytest.fixture
def data_dir(tmp_path):
    """A copy of the shipped catalog in a tmp dir, so a test can edit it freely.

    A copy rather than the real directory because several tests delete or rewrite files in it to
    check the startup error messages, and they must not be able to damage the repository's data.
    """
    target = tmp_path / "data"
    (target / "surfaces").mkdir(parents=True)
    (target / "maps").mkdir(parents=True)
    for name in ("rooms.json", "patients.json"):
        (target / name).write_text((REPO_DATA / name).read_text(encoding="utf-8"),
                                   encoding="utf-8")
    for obj in (REPO_DATA / "surfaces").glob("*.obj"):
        (target / "surfaces" / obj.name).write_bytes(obj.read_bytes())
    # The recorded maps too, or the recorded-map code path is untested and anything that asks
    # for a recording silently gets a computed one instead.
    for recorded in (REPO_DATA / "maps").iterdir():
        if recorded.is_file():
            (target / "maps" / recorded.name).write_bytes(recorded.read_bytes())
    return target


@pytest.fixture
def make_settings(data_dir, tmp_path):
    """Build settings against the temporary catalog; kwargs override any field."""
    def _make(**overrides):
        # env_prefix means a MAPRT_EMULATOR_* variable in the developer's shell would otherwise leak
        # into every test. Passing these explicitly pins them.
        base = {"token": TOKEN, "data_dir": data_dir, "state_dir": tmp_path / ".maprt_emulator",
                "host": "127.0.0.1", "port": 8000, "unlicensed": (), "latency_ms": 0,
                "auth_401": False, "strict_user_agent": True, "open_ping": False,
                "packs_dir": None, "packs": (), "no_packs": False}
        base.update(overrides)
        return EmulatorSettings(**base)
    return _make


@pytest.fixture
def client_factory(make_settings):
    """A function returning a client wired straight to the app — no socket, no server process.

    ``token=None`` sends no Authorization header; ``user_agent=None`` sends no User-Agent.
    Any remaining keyword overrides a settings field, so a test can flip ``auth_401`` or
    ``unlicensed`` without building its own app.
    """
    def _factory(token=TOKEN, user_agent="VisionRT.Integration.Saturn/2.0.tests", **settings_kw):  # audit: allow path-construction - a User-Agent, not a path
        app = create_app(make_settings(**settings_kw))
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        # TestClient sends a default User-Agent of its own ("testclient"), which would make
        # user_agent=None indistinguishable from a client that did send one. Setting it empty is
        # the closest the client library allows to omitting the header.
        else:
            headers["User-Agent"] = ""
        return TestClient(app, base_url="http://maprt.test", headers=headers)
    return _factory


@pytest.fixture
def client(client_factory):
    """An authenticated client against the shipped catalog."""
    with client_factory() as c:
        yield c


@pytest.fixture
def token():
    """The token the fixtures authenticate with."""
    return TOKEN


#: The recorded envelopes' keys, in the appliance's order. A failure has no ``data`` key.
SUCCESS_KEYS = ["data", "errorData", "success", "errorCode", "errorCodeType"]
ERROR_KEYS = ["errorData", "success", "errorCode", "errorCodeType"]


@pytest.fixture
def envelope_of():
    """Parse a response as the MapRT envelope, asserting it has exactly the recorded keys.

    Checking the shape on every read means a route that returns a bare dict, FastAPI's own
    ``{"detail": …}``, or a key an appliance never sends, fails the test that touches it rather
    than only the one that looks.
    """
    def _parse(response):
        body = json.loads(response.text)
        expected = SUCCESS_KEYS if body.get("success") is True else ERROR_KEYS
        assert list(body) == expected, f"not a recorded MapRT envelope: {list(body)}"
        return body
    return _parse


# -- module-scoped variants --------------------------------------------------------------------
#
# For modules that only read. Building the app per test means a fresh MapCache per test, so a
# high-resolution map — about a second of CPU — gets recomputed for every one of them. Sharing
# one app across a module took the beam-agreement suite from 28s to about 3s.
#
# Only safe for tests that do not mutate server state. A test that arms a scenario or edits the
# catalog must use the function-scoped `client` / `client_factory` instead, or it will leak into
# whatever runs after it.

@pytest.fixture(scope="module")
def shared_data_dir(tmp_path_factory):
    """A copy of the shipped catalog, shared by a whole test module. Do not modify it."""
    target = tmp_path_factory.mktemp("shared-data") / "data"
    (target / "surfaces").mkdir(parents=True)
    (target / "maps").mkdir(parents=True)
    for name in ("rooms.json", "patients.json"):
        (target / name).write_text((REPO_DATA / name).read_text(encoding="utf-8"),
                                   encoding="utf-8")
    for obj in (REPO_DATA / "surfaces").glob("*.obj"):
        (target / "surfaces" / obj.name).write_bytes(obj.read_bytes())
    for recorded in (REPO_DATA / "maps").iterdir():
        if recorded.is_file():
            (target / "maps" / recorded.name).write_bytes(recorded.read_bytes())
    return target


@pytest.fixture(scope="module")
def shared_client(shared_data_dir, tmp_path_factory):
    """An authenticated client on one app for the whole module, with no stream pacing."""
    settings = EmulatorSettings(
        token=TOKEN, data_dir=shared_data_dir,
        state_dir=tmp_path_factory.mktemp("shared-state"),
        host="127.0.0.1", port=8000, unlicensed=(), latency_ms=0, map_seconds=0.0,
        auth_401=False, strict_user_agent=True, open_ping=False,
    )
    app = create_app(settings)
    with TestClient(app, base_url="http://maprt.test", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "User-Agent": "VisionRT.Integration.Saturn/2.0.tests",  # audit: allow path-construction - a User-Agent, not a path
    }) as client:
        yield client
