"""Start the real server as a subprocess and check the flags actually took effect.

This exists because of a bug the unit tests could not see. `settings_from_args` was correct and
`create_app(settings)` was correct, but `main()` handed uvicorn an *import string* — so the
server process rebuilt its settings from the environment and dropped every command-line flag,
while the banner cheerfully claimed they were in effect. Testing the two halves separately can
never catch that; only running the actual command can.

One server process is started per module, with several non-default flags set at once, and each
test asserts that its flag reached the request path.
"""

import json
import socket
import subprocess
import sys
import time

import httpx2
import pytest

TOKEN = "integration-test-token"
UA = {"User-Agent": "VisionRT.Integration.Saturn/2.0.tests"}  # audit: allow path-construction - a User-Agent, not a path
SURFACE = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A real `maprt-emulator` process with several non-default flags.

    Yields ``(base_url, workdir)``. ``workdir`` holds the ``.maprt_emulator/`` the server wrote its
    token into — named that way because it is what a client library walks up looking
    for, and a test asserts the two agree.
    """
    port = _free_port()
    workdir = tmp_path_factory.mktemp("workdir")
    state = workdir / ".maprt_emulator"
    process = subprocess.Popen(  # audit: allow subprocesses - sys.executable -m, the venv's own interpreter
        [sys.executable, "-m", "maprt_emulator",
         "--port", str(port),
         "--token", TOKEN,
         "--state-dir", str(state),
         "--unlicensed", "surfaces",
         "--lenient-user-agent",
         "--auth-401",
         "--latency-ms", "10",
         "--map-seconds", "1",
         "--source-headers"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"maprt-emulator exited early:\n{process.stdout.read()}")
        with socket.socket() as probe:
            probe.settimeout(0.5)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.2)
    else:
        process.terminate()
        pytest.fail("maprt-emulator did not start within 30s")

    yield base, workdir

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


@pytest.fixture(scope="module")
def base(server):
    """Just the base URL, for the tests that do not care about the working directory."""
    return server[0]


def get(base, path, token=TOKEN, **kwargs):
    headers = dict(UA)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    headers.update(kwargs.pop("headers", {}))
    return httpx2.get(base + path, headers=headers, timeout=30, **kwargs)


class TestTheServerIsReallyServing:
    def test_ping_answers_over_a_socket(self, base):
        response = get(base, "/integration/ping")
        assert response.status_code == 200
        assert json.loads(response.text)["data"] == "2.0.0"

    def test_the_saturn_server_header_survives_uvicorn(self, base):
        # uvicorn sets its own Server header; the middleware has to win. Only a real server
        # process can show whether it does.
        assert get(base, "/integration/ping").headers["server"] == "Saturn/1.3.217.2"


class TestFlagsReachTheRequestPath:
    def test_lenient_user_agent_is_in_effect(self, base):
        # THE regression this class exists for: a flag the banner reported but the server process
        # never saw. Without --lenient-user-agent, httpx2's own User-Agent gets VersionMismatch.
        response = httpx2.get(base + "/integration/ping",
                             headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30)
        assert response.status_code == 200
        assert json.loads(response.text)["success"] is True

    def test_unlicensed_is_in_effect(self, base):
        body = json.loads(get(base, f"/integration/surfaces/{SURFACE}").text)
        assert body["success"] is False
        assert body["errorCode"] == "FeatureNotLicensed"

    def test_auth_401_is_in_effect(self, base):
        # Without the flag this would be the spec default: HTTP 200 with success:false.
        assert get(base, "/integration/rooms", token="wrong").status_code == 401

    def test_the_token_flag_is_in_effect(self, base):
        assert json.loads(get(base, "/integration/rooms").text)["success"] is True

    def test_the_data_directory_was_found_by_walking_up(self, base):
        # No --data-dir was passed, so the server had to locate the repository's data/ itself.
        rooms = json.loads(get(base, "/integration/rooms").text)["data"]
        assert len(rooms) == 4


class TestGetMapReallyStreams:
    """That the map body arrives progressively — only observable over a real socket.

    The in-process test client buffers the whole response, so this cannot be checked in
    ``test_getmap.py``. It matters because the format exists to be streamed: a real appliance
    takes minutes for a high-resolution map and sends it as it computes, which is what lets a
    client show progress instead of appearing to hang.
    """

    @staticmethod
    def map_body(**overrides):
        out = {"PatientSurfaceId": SURFACE,
               "TreatmentRoomId": "96bbc235-0da9-e983-0783-5973a7d11144",
               "PatientPosition": "HFS", "CouchBuffer": 20.0, "PatientBuffer": 20.0,
               "HighResolution": True, "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0}}
        out.update(overrides)
        return out

    def test_the_body_arrives_in_many_chunks(self, base):
        with httpx2.Client(timeout=120) as client, client.stream("POST", base + "/integration/GetMap",
                           json=self.map_body(), headers=_map_headers()) as response:
            chunks = [chunk for chunk in response.iter_raw() if chunk]
        assert len(chunks) > 10, f"only {len(chunks)} chunk(s) — the response was buffered"

    def test_the_first_chunk_is_the_header_line_alone(self, base):
        # A client can then read the sampling before any data arrives.
        with httpx2.Client(timeout=120) as client, client.stream("POST", base + "/integration/GetMap",
                           json=self.map_body(), headers=_map_headers()) as response:
            first = next(chunk for chunk in response.iter_raw() if chunk)
        assert first.decode().strip() == "181,360"

    def test_the_whole_body_still_parses(self, base):
        import client_wire as wire
        with httpx2.Client(timeout=120) as client:
            response = client.post(base + "/integration/GetMap",
                                   json=self.map_body(), headers=_map_headers())
        image, _x, _y = wire.parse_map_response(response.text)
        assert image.shape == (360, 181)

    def test_the_map_source_header_is_sent(self, base):
        with httpx2.Client(timeout=120) as client:
            response = client.post(base + "/integration/GetMap",
                                   json=self.map_body(), headers=_map_headers())
        assert response.headers["X-Emulator-Map-Source"] in {"recorded", "recorded-nearest",
                                                        "synthetic"}


def _map_headers():
    return {**UA, "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


class TestTokenFile:
    def test_is_written_in_the_state_folder_of_the_working_directory(self, server):
        # Clients depend on this file, not on the banner — on Binder and Colab the banner goes to
        # a log nobody reads — so the running server has to write it where they look.
        _base, workdir = server
        assert (workdir / ".maprt_emulator" / "token").read_text(encoding="utf-8").strip() == TOKEN
