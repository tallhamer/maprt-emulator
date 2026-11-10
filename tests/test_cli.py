"""The CLI: token generation and recording, flag plumbing, and the banner."""

import io
import stat
import sys

import pytest

from maprt_emulator.cli import (
    _elide,
    _parser,
    _stdout_can_print_the_banner,
    banner,
    list_scenarios,
    main,
    settings_from_args,
)
from maprt_emulator.scenarios import SCENARIOS
from maprt_emulator.settings import EmulatorSettings
from maprt_emulator.store import Catalog


def parse(*argv):
    return settings_from_args(_parser().parse_args(argv))


class TestToken:
    def test_a_fresh_token_is_generated_when_none_is_given(self, tmp_path):
        settings = EmulatorSettings(token=None, state_dir=tmp_path / "state")
        token = settings.resolve_token()
        assert token and len(token) > 20

    def test_two_starts_generate_different_tokens(self, tmp_path):
        # The point of generating one per start: a workshop credential is disposable, and nobody
        # should end up with a long-lived shared secret in their notes.
        first = EmulatorSettings(token=None, state_dir=tmp_path).resolve_token()
        second = EmulatorSettings(token=None, state_dir=tmp_path).resolve_token()
        assert first != second

    def test_an_explicit_token_is_respected(self, tmp_path):
        assert EmulatorSettings(token="pinned", state_dir=tmp_path).resolve_token() == "pinned"

    def test_is_written_to_the_state_directory(self, tmp_path):
        settings = EmulatorSettings(token="pinned", state_dir=tmp_path / "state")
        path = settings.write_token()
        assert path == tmp_path / "state" / "token"
        assert path.read_text(encoding="utf-8").strip() == "pinned"

    @pytest.mark.skipif(sys.platform == "win32", reason="Windows has no POSIX mode bits; chmod is best effort")
    def test_the_token_file_is_owner_readable_only(self, tmp_path):
        # It is a throwaway credential for a fake appliance, but a workshop that handles the
        # disposable one sloppily teaches handling the real one sloppily.
        path = EmulatorSettings(token="pinned", state_dir=tmp_path / "state").write_token()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


class TestFlags:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("MAPRT_EMULATOR_TOKEN", raising=False)
        settings = parse()
        # 127.0.0.1, not 0.0.0.0: a printed token and fake clinical data have no business being
        # reachable from the rest of the network by default.
        assert settings.host == "127.0.0.1"
        assert settings.port == 8000
        assert settings.unlicensed == ()
        assert settings.auth_401 is False
        # Strict, as the appliance is: a bad User-Agent gets VersionMismatch.
        assert settings.strict_user_agent is True

    def test_host_and_port(self):
        settings = parse("--host", "0.0.0.0", "--port", "9001")
        assert (settings.host, settings.port) == ("0.0.0.0", 9001)

    def test_unlicensed_is_split_on_commas(self):
        assert parse("--unlicensed", "surfaces,map").unlicensed == ("surfaces", "map")

    def test_an_unknown_feature_is_rejected_with_the_valid_choices(self):
        with pytest.raises(SystemExit, match="surfaces"):
            parse("--unlicensed", "telepathy")

    def test_store_true_flags(self):
        settings = parse("--auth-401", "--open-ping", "--source-headers")
        assert settings.auth_401 and settings.open_ping and settings.source_headers

    def test_source_headers_are_off_unless_asked_for(self):
        # An appliance sends no X-Emulator-* header, so a client must not find one by default.
        assert parse().source_headers is False

    def test_lenient_user_agent_turns_the_check_off(self):
        assert parse("--lenient-user-agent").strict_user_agent is False

    def test_the_environment_can_turn_it_off_too(self, monkeypatch):
        monkeypatch.setenv("MAPRT_EMULATOR_STRICT_USER_AGENT", "false")
        assert parse().strict_user_agent is False

    def test_unset_flags_leave_the_environment_in_charge(self, monkeypatch):
        # So the Binder/Colab bootstraps can configure by env var without passing flags.
        monkeypatch.setenv("MAPRT_EMULATOR_PORT", "7777")
        assert parse().port == 7777

    def test_an_explicit_flag_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv("MAPRT_EMULATOR_PORT", "7777")
        assert parse("--port", "8123").port == 8123


class TestScenarioFlags:
    def test_scenario_defaults_to_ok(self, monkeypatch):
        monkeypatch.delenv("MAPRT_EMULATOR_SCENARIO", raising=False)
        assert parse().scenario == "ok"

    def test_a_named_scenario_is_armed_for_the_run(self):
        assert parse("--scenario", "unlicensed-map").scenario == "unlicensed-map"

    def test_an_unknown_scenario_is_rejected_at_the_prompt(self, capsys):
        with pytest.raises(SystemExit):
            parse("--scenario", "explode")
        assert "unlicensed-map" in capsys.readouterr().err

    def test_list_scenarios_describes_each_and_exits_without_serving(self, capsys):
        main(["--list-scenarios"])
        out = capsys.readouterr().out
        assert out == list_scenarios() + "\n"
        for name, scenario in SCENARIOS.items():
            assert name in out and scenario.description in out


class TestBanner:
    def test_shows_the_url_token_and_data_summary(self, data_dir, tmp_path):
        settings = EmulatorSettings(token="shown-token", data_dir=data_dir, state_dir=tmp_path,
                               port=8123)
        text = banner(settings, Catalog.load(data_dir), tmp_path / "token")
        assert "http://127.0.0.1:8123" in text
        assert "MAPRT_EMULATOR_TOKEN=shown-token" in text
        assert "3 patients" in text
        assert "NOT a medical device" in text

    def test_warns_about_non_default_behaviour(self, data_dir, tmp_path):
        # Someone debugging a confusing response needs to know the server was told to be weird.
        settings = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path,
                               unlicensed=("map",), auth_401=True, open_ping=True)
        text = banner(settings, Catalog.load(data_dir), tmp_path / "token")
        assert "Unlicensed" in text and "map" in text
        assert "401" in text
        assert "WITHOUT a token" in text

    def test_names_an_armed_scenario(self, data_dir, tmp_path):
        settings = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path,
                                    scenario="throttle")
        text = banner(settings, Catalog.load(data_dir), tmp_path / "token")
        assert "Scenario" in text and "throttle" in text

    def test_warns_when_source_headers_are_sent(self, data_dir, tmp_path):
        settings = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path,
                                    source_headers=True)
        assert "X-Emulator-*" in banner(settings, Catalog.load(data_dir), tmp_path / "token")

    def test_says_docs_calls_need_a_lenient_user_agent(self, data_dir, tmp_path):
        # A browser can't send the Saturn User-Agent, so /docs calls fail on a strict server.
        strict = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path)
        lenient = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path,
                                   strict_user_agent=False)
        catalog = Catalog.load(data_dir)
        assert "Try it out needs --lenient-user-agent" in banner(strict, catalog, tmp_path / "token")
        assert "Try it out" not in banner(lenient, catalog, tmp_path / "token")

    def test_the_box_is_not_ragged(self, data_dir, tmp_path):
        settings = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path)
        lines = [line for line in banner(settings, Catalog.load(data_dir), tmp_path).splitlines()
                 if line.startswith(("╭", "│", "╰"))]
        assert len({len(line) for line in lines}) == 1


class TestPrintingTheBanner:
    def test_a_cp1252_stdout_is_switched_to_utf8(self, data_dir, tmp_path, monkeypatch):
        # A piped stdout on Windows is cp1252, which has no box-drawing characters.
        raw = io.BytesIO()
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
        _stdout_can_print_the_banner()
        settings = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path)
        print(banner(settings, Catalog.load(data_dir), tmp_path / "token"), flush=True)
        assert "╭" in raw.getvalue().decode("utf-8")

    def test_a_utf8_stdout_is_left_alone(self, monkeypatch):
        stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
        monkeypatch.setattr(sys, "stdout", stream)
        _stdout_can_print_the_banner()
        assert (stream.encoding, stream.errors) == ("utf-8", "strict")


class TestElide:
    def test_a_short_path_is_untouched(self):
        assert _elide("/short/data", 58) == "/short/data"

    def test_a_long_path_keeps_the_tail(self):
        # The tail is the informative part — which data directory this is.
        out = _elide("/Users/someone/very/deeply/nested/project/tree/here/data", 30)  # audit: allow writable-locations - a string under test, not a file
        assert len(out) <= 30
        assert out.startswith("...")
        assert out.endswith("/data")

    def test_dropping_one_component_is_enough_for_a_deep_path(self):
        # A deep path with a short tail elides to almost nothing, which is fine — the directory
        # name is what the reader needs, and the full path is in the error messages if it matters.
        assert _elide("/" + "a" * 200 + "/data", 30) == ".../data"

    def test_a_path_with_no_separators_falls_back_to_a_front_cut(self):
        # Nothing to drop, so the fallback branch has to bound it some other way.
        out = _elide("a" * 200, 30)
        assert len(out) == 30
        assert out.startswith("...")

    def test_a_single_over_long_component_is_still_bounded(self):
        out = _elide("/".join(["", "home", "b" * 200]), 30)
        assert len(out) == 30


class TestAsEnv:
    """The ``--reload`` path's only channel to the server process.

    Covered because the normal path deliberately does *not* use it: see the comment in
    ``cli.main``. If a field is ever added that cannot survive the environment, --reload would
    silently drop it, which is the bug class this whole area already produced once.
    """

    def test_every_field_round_trips(self, monkeypatch, data_dir, tmp_path):
        original = EmulatorSettings(token="t", data_dir=data_dir, state_dir=tmp_path / "s",
                               host="0.0.0.0", port=9100, unlicensed=("surfaces", "map"),
                               latency_ms=25, auth_401=True, strict_user_agent=True,
                               open_ping=True)
        # monkeypatch, not os.environ.update: as_env() is pure, so the test has to be the thing
        # that scopes the change. Writing straight to os.environ here once leaked these settings
        # into every later test and into the subprocesses they start.
        for key, value in original.as_env().items():
            monkeypatch.setenv(key, value)
        restored = EmulatorSettings()
        for field in type(original).model_fields:
            assert getattr(restored, field) == getattr(original, field), field

    def test_false_booleans_round_trip_as_false(self, monkeypatch, tmp_path):
        # "False" must not come back truthy, which a naive str() export would produce.
        for key, value in EmulatorSettings(token="t", state_dir=tmp_path,
                                      auth_401=False).as_env().items():
            monkeypatch.setenv(key, value)
        assert EmulatorSettings().auth_401 is False

    def test_an_empty_unlicensed_tuple_round_trips(self, monkeypatch, tmp_path):
        for key, value in EmulatorSettings(token="t", state_dir=tmp_path,
                                      unlicensed=()).as_env().items():
            monkeypatch.setenv(key, value)
        assert EmulatorSettings().unlicensed == ()
