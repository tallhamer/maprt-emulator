"""Bad requests get the answer a real appliance gives, status and body byte for byte.

Each case in ``data/recorded_bad_requests.json`` is a request sent to a real MapRT appliance and
what it answered, from Session Recordings (2026-10-23): a wrong or malformed token, an unacceptable
User-Agent, an unknown patient, surface or room, an ambiguous or over-long machine name, and a
GetMap body without PatientPosition. ``scripts/extract_bad_request_fixtures.py`` writes the file;
placeholders in ``<>`` stand for site values and are filled here from the emulator's catalog.
"""

import json
from pathlib import Path

import pytest

FIXTURE_PATH = Path(__file__).parent / "data" / "recorded_bad_requests.json"  # audit: allow writable-locations - read-only test data
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
CASES = FIXTURE["cases"]

TOKEN = "test-token-not-a-real-secret"

#: The emulator's built-in catalog, standing in for the site's values.
PLACEHOLDERS = {
    "<KNOWN_PATIENT>": "ANON001",
    "<UNKNOWN_PATIENT>": "ANON999",
    "<KNOWN_SURFACE>": "dad7394f-6893-4ce3-96ed-8bc3091cf4c3",
    "<KNOWN_ROOM>": "96bbc235-0da9-e983-0783-5973a7d11144",
    "<SHARED_MACHINE>": "TrueBeam",          # two rooms share it
    "<UNIQUE_MACHINE>": "TrueBeam2",
    "<UNIQUE_ROOM_ID>": "4d1f6e02-7c3b-4a19-9f55-2b8e0c71a3d7",
}

AUTHORIZATION = {
    "valid": f"Bearer {TOKEN}",
    "wrong": f"Bearer {TOKEN}x",
    "bearer-only": "Bearer",
    "token-only": TOKEN,
    "none": None,
}


def fill(value):
    if isinstance(value, str):
        for placeholder, real in PLACEHOLDERS.items():
            value = value.replace(placeholder, real)
        return value
    if isinstance(value, dict):
        return {key: fill(item) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item) for item in value]
    return value


@pytest.fixture(scope="module")
def raw_client(shared_data_dir, tmp_path_factory):
    """A client that sends only the headers a case gives it: no token, and no User-Agent of its own."""
    from fastapi.testclient import TestClient

    from maprt_emulator.app import create_app
    from maprt_emulator.settings import EmulatorSettings

    settings = EmulatorSettings(token=TOKEN, data_dir=shared_data_dir,
                                state_dir=tmp_path_factory.mktemp("state"), host="127.0.0.1",
                                port=8000, unlicensed=(), latency_ms=0, auth_401=False,
                                strict_user_agent=True, open_ping=False, map_seconds=0.0)
    with TestClient(create_app(settings)) as client:
        client.headers.pop("user-agent", None)
        yield client


def test_every_kind_of_bad_request_was_recorded():
    # If a re-extraction loses a category, the emulator stops being checked against it.
    codes = {case["expected"].get("body", {}).get("errorCode") for case in CASES}
    assert {"InvalidClientToken", "VersionMismatch", "PatientNotFoundInDatabase", "SurfaceNotFound",
            "TreatmentRoomNotFound", "DuplicateKey"} <= codes
    assert any(case["expected"].get("map") for case in CASES)


@pytest.mark.parametrize("case", CASES, ids=[case["source"] for case in CASES])
def test_the_emulator_answers_as_the_appliance_did(raw_client, case):
    headers = {"Content-Type": "application/json"}
    if AUTHORIZATION[case["authorization"]] is not None:
        headers["Authorization"] = AUTHORIZATION[case["authorization"]]
    if case["user_agent"] is not None:
        headers["User-Agent"] = case["user_agent"]

    path, body = fill(case["path"]), fill(case["body"])
    if case["method"] == "POST":
        response = raw_client.post(path, json=body, headers=headers)
    else:
        response = raw_client.get(path, headers=headers)

    expected = case["expected"]
    assert response.status_code == expected["status"]
    if expected.get("map"):
        # A GetMap the appliance computed although the body was incomplete: map text, not JSON.
        lines = response.text.splitlines()
        assert lines[0].count(",") == 1 and lines[-1] == "DONE"
        assert "content-type" not in response.headers
    else:
        # The exact bytes the appliance sent: the same keys, in the same order, compact.
        recorded = json.dumps(fill(expected["body"]), separators=(",", ":")).encode()
        assert response.content == recorded
