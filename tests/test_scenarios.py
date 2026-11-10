"""Fault injection: every armed scenario actually does what it advertises.

A scenario catalogue that lies is worse than none — a student would conclude their error handling
works because the fault never fired. So each one is armed and the effect observed.
"""

import json

import pytest
from pydantic import ValidationError

from maprt_emulator import scenarios

SURFACE = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"
ROOM = "96bbc235-0da9-e983-0783-5973a7d11144"


def map_body(**overrides):
    out = {"PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "PatientPosition": "HFS",
           "CouchBuffer": 20.0, "PatientBuffer": 20.0, "HighResolution": False,
           "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0}}
    out.update(overrides)
    return out


def beam_body():
    return dict(map_body(), BeamList=[
        {"beamNumber": 1, "controlPoints": [
            {"gantryAngle": 0, "couchAngle": 0, "coordinateSystem": "IEC_61217"}]}])


@pytest.fixture
def api(client_factory):
    with client_factory(map_seconds=0.0) as client:
        yield client


@pytest.fixture
def started(client_factory):
    """Start an emulator with a scenario armed, as ``--scenario NAME`` does."""
    clients = []

    def _start(name, **settings):
        client = client_factory(map_seconds=0.0, scenario=name, **settings)
        clients.append(client)
        return client.__enter__()
    yield _start
    for client in clients:
        client.__exit__(None, None, None)


class TestTheCatalogue:
    def test_lists_every_scenario_with_what_it_teaches(self):
        listed = scenarios.catalogue()
        assert {s["name"] for s in listed} == set(scenarios.SCENARIOS)
        for entry in listed:
            assert entry["description"]
            assert entry["teaches"], f"{entry['name']} does not say what it is for"

    def test_ok_is_the_empty_scenario(self):
        ok = scenarios.SCENARIOS["ok"]
        assert ok.unlicensed == () and not ok.reject_token and not ok.forced_status
        assert not ok.extra_delay and not ok.throttle_failures
        assert not ok.truncate_map and not ok.shuffle_map

    def test_an_unknown_name_is_refused_with_the_available_ones(self, make_settings):
        with pytest.raises(ValidationError) as exc:
            make_settings(scenario="explode")
        assert "unlicensed-map" in str(exc.value)


class TestOnlyTheApiIsServed:
    """No route to arm a scenario: a real appliance has none. FastAPI's own pages stay."""

    @pytest.mark.parametrize("path", ["/emulator/state", "/emulator/scenarios"])
    def test_non_api_paths_are_not_found(self, api, path):
        assert api.get(path).status_code == 404

    @pytest.mark.parametrize("path", ["/docs", "/redoc",
                                      "/openapi.json"])  # audit: allow writable-locations - a URL path, not a file
    def test_fastapis_own_pages_answer(self, api, path):
        assert api.get(path).status_code == 200

    def test_arming_over_http_is_not_possible(self, api):
        assert api.post("/emulator/scenario", json={"name": "bad-token"}).status_code == 404
        assert json.loads(api.get("/integration/ping").text)["success"] is True

    def test_the_default_is_ok(self, api, envelope_of):
        assert api.app.state.scenarios.armed.name == "ok"
        assert envelope_of(api.get("/integration/ping"))["success"] is True


class TestEachScenarioFires:
    def test_bad_token_rejects_a_correct_token(self, started, envelope_of):
        api = started("bad-token")
        body = envelope_of(api.get("/integration/rooms"))
        assert body["success"] is False
        assert body["errorCode"] == "InvalidClientToken"

    def test_unlicensed_surfaces_blocks_the_mesh_but_not_the_listing(self, started, envelope_of):
        api = started("unlicensed-surfaces")
        assert envelope_of(api.get(f"/integration/surfaces/{SURFACE}"))["errorCode"] \
            == "FeatureNotLicensed"
        assert envelope_of(api.get("/integration/patients/ANON001/surfaces"))["success"] is True

    def test_unlicensed_map_blocks_getmap(self, started, envelope_of):
        api = started("unlicensed-map")
        response = api.post("/integration/GetMap", json=map_body())
        assert response.status_code == 200
        assert envelope_of(response)["errorCode"] == "FeatureNotLicensed"

    def test_unlicensed_beams_blocks_the_beam_endpoint_only(self, started, envelope_of):
        api = started("unlicensed-beams")
        assert envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                    json=beam_body()))["errorCode"] == "FeatureNotLicensed"
        # The map is a different licence, so it keeps working.
        assert api.post("/integration/GetMap", json=map_body()).status_code == 200

    def test_server_error_gives_a_500(self, started):
        api = started("server-error")
        assert api.get("/integration/rooms").status_code == 500

    def test_the_500_still_carries_the_envelope(self, started, envelope_of):
        # A real 5xx may carry no envelope at all; one is sent here so the status is the thing
        # being exercised rather than the body.
        api = started("server-error")
        assert envelope_of(api.get("/integration/rooms"))["success"] is False

    def test_truncate_map_stops_before_DONE(self, started):
        api = started("truncate-map")
        text = api.post("/integration/GetMap", json=map_body()).text
        assert "DONE" not in text

    def test_the_client_parser_refuses_a_truncated_map(self, api, started):
        # The lesson. The parser infers the gantry axis from the values actually present, so
        # before maprt-client 0.1.1 a half-delivered map parsed happily into a HALF-HEIGHT array,
        # and code indexing by gantry row then read some other angle than it asked for. This is
        # why DONE exists: the parser now refuses a reply without it.
        import client_wire as wire
        complete, _x, _y = wire.parse_map_response(
            api.post("/integration/GetMap", json=map_body()).text)
        assert complete.shape == (360, 181)
        with pytest.raises(wire.IncompleteMapError, match="DONE"):
            wire.parse_map_response(started("truncate-map").post("/integration/GetMap", json=map_body()).text)

    def test_shuffle_map_changes_the_order_but_not_the_map(self, api, started):
        import client_wire as wire
        import numpy as np
        ordered = api.post("/integration/GetMap", json=map_body()).text
        shuffled = started("shuffle-map").post("/integration/GetMap", json=map_body()).text
        assert ordered != shuffled
        assert np.array_equal(wire.parse_map_response(ordered)[0],
                              wire.parse_map_response(shuffled)[0])

    def test_throttle_returns_503_with_retry_after_then_recovers(self, started):
        # Recovery is the point: a server that throttled forever would teach students to write a
        # retry loop that can never exit.
        api = started("throttle")
        first = api.get("/integration/ping")
        assert first.status_code == 503
        assert first.headers["Retry-After"] == "1"
        assert api.get("/integration/ping").status_code == 503     # the second of two
        assert api.get("/integration/ping").status_code == 200     # recovered

    def test_a_retry_loop_with_backoff_succeeds(self, started):
        # What a student is meant to write, working end to end.
        api = started("throttle")
        for _attempt in range(5):
            response = api.get("/integration/ping")
            if response.status_code != 503:
                break
        assert response.status_code == 200

    def test_slow_delays_every_api_call(self, started):
        import time
        # 8 s would make the suite tedious, so check the delay is applied rather than its length:
        # the scenario's own field is the contract, and the middleware reads it.
        assert scenarios.SCENARIOS["slow"].extra_delay == 8.0
        api = started("slow")
        api.app.state.scenarios.armed = scenarios.Scenario("slow-test", "test", extra_delay=0.3)
        started_at = time.monotonic()
        api.get("/integration/ping")
        # Windows timers tick every ~15.6 ms, so asyncio.sleep(0.3) can wake a tick early (0.297 s
        # was seen on CI). A margin of a few ticks still proves the delay was applied.
        assert time.monotonic() - started_at >= 0.25


class TestTheLaunchTimeScenario:
    def test_it_stays_armed_for_the_whole_run(self, started, envelope_of):
        api = started("unlicensed-map")
        for _ in range(3):
            assert envelope_of(api.post("/integration/GetMap", json=map_body()))["errorCode"] \
                == "FeatureNotLicensed"

    def test_it_adds_to_the_command_line_unlicensed_flag(self, started, envelope_of):
        # A scenario adds to --unlicensed; it never re-licenses something the operator switched
        # off at startup.
        api = started("unlicensed-beams", unlicensed=("map",))
        assert envelope_of(api.post("/integration/GetMap", json=map_body()))["errorCode"] \
            == "FeatureNotLicensed"
        assert envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                    json=beam_body()))["errorCode"] == "FeatureNotLicensed"

    def test_the_environment_can_choose_it(self, monkeypatch, tmp_path):
        # How Binder and Colab start the emulator: no flags, only MAPRT_EMULATOR_* variables.
        from maprt_emulator.settings import EmulatorSettings
        monkeypatch.setenv("MAPRT_EMULATOR_SCENARIO", "throttle")
        assert EmulatorSettings(token="t", state_dir=tmp_path).scenario == "throttle"
