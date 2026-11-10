"""How the emulator is configured, and how it finds its data and writes its token.

Every field can be set three ways, in increasing priority: a default here, a
``MAPRT_EMULATOR_``-prefixed environment variable (how the Binder and Colab bootstraps do it), or a
command-line flag (:mod:`maprt_emulator.cli`).
"""

import datetime as dt
import json
import os
import secrets
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from maprt_emulator.scenarios import SCENARIOS

#: Reported in the ``Server`` header, as a real appliance reports its build (spec §5.1). The
#: value the recorded appliance sent (2026-10-23).
SERVER_VERSION = "Saturn/1.3.217.2"

#: Reported by ``/integration/ping``. This is the *API* version, which the spec is careful to
#: keep separate from the product version above (§3.3).
API_VERSION = "2.0.0"

#: What a client's User-Agent starts with, before the version (spec §5.1).
USER_AGENT_PREFIX = "VisionRT.Integration.Saturn/"

#: Features a real appliance licenses separately (§2.3.3), and which ``--unlicensed`` can switch
#: off here so a notebook can see ``FeatureNotLicensed`` without one.
LICENSABLE = ("surfaces", "map", "beams")


#: Where a project keeps its MapRT catalog, relative to its root — the SGRT Scripting Workshop's
#: layout, where ``data/`` holds one folder per Vision RT product, each with its own git-ignored
#: ``local/`` overlay.
DATA_SUBDIR = Path("data") / "MapRT"

#: The small synthetic catalog shipped inside the package: what an emulator started anywhere
#: else serves, so ``pip install`` then ``maprt-emulator`` works with no data of your own.
BUILTIN_DATA_DIR = Path(__file__).resolve().parent / "data"  # audit: allow writable-locations - package data, only ever read


def find_data_dir(start=None):
    """The nearest ``data/MapRT/`` above ``start`` (cwd by default), else the built-in catalog.

    Walking up rather than trusting the working directory means ``maprt-emulator`` behaves the same
    whether it is started from a project's root or from its ``notebooks/`` folder.
    """
    here = Path(start or Path.cwd()).resolve()
    for directory in [here, *here.parents]:
        candidate = directory / DATA_SUBDIR
        if (candidate / "rooms.json").is_file():
            return candidate
    return BUILTIN_DATA_DIR


class EmulatorSettings(BaseSettings):
    """Emulator configuration."""

    model_config = SettingsConfigDict(env_prefix="MAPRT_EMULATOR_", extra="ignore")

    # -- network ------------------------------------------------------------------------------
    # 127.0.0.1, not 0.0.0.0: this thing has a printed token and fake clinical data, and has no
    # business being reachable from the rest of the network by default.
    host: str = "127.0.0.1"
    port: int = 8000

    # -- auth ---------------------------------------------------------------------------------
    # None means "generate a fresh one at startup". Set MAPRT_EMULATOR_TOKEN to pin it, which is what
    # Binder and Colab do because neither has a terminal banner anyone will read.
    token: str | None = None

    #: Reject a token problem with HTTP 401 instead of the appliance's 200 + ``success:false``.
    #: Not what the recorded appliance does; it exercises a client's other branch.
    auth_401: bool = False

    #: Answer an unacceptable ``User-Agent`` with ``VersionMismatch``, as the appliance does
    #: (:mod:`maprt_emulator.auth`). On by default; ``--lenient-user-agent`` turns it off, which
    #: only logs, so a first lesson's bare GET can work before the header has been taught.
    strict_user_agent: bool = True

    #: Serve ``/integration/ping`` without a token, for debugging connectivity in isolation.
    open_ping: bool = False

    #: Send ``X-Emulator-Map-Source`` / ``X-Emulator-Beam-Source``, naming which source answered.
    #: Off by default, because an appliance sends no such header and a client must not come to
    #: look for one. The source is always in the server log.
    source_headers: bool = False

    # -- data ---------------------------------------------------------------------------------
    data_dir: Path = Field(default_factory=find_data_dir)

    #: A folder of session packs to serve (``docs/PACK_FORMAT.md``): every ``.zip`` and every folder
    #: with a ``manifest.json`` directly inside it. ``None`` serves the catalog in :attr:`data_dir`.
    packs_dir: Path | None = None

    #: Serve only these packs from :attr:`packs_dir`, by manifest name. Empty means every pack there.
    packs: tuple[str, ...] = ()

    #: Read no pack, whatever :attr:`packs_dir` says (say, set in the environment): serve the catalog.
    no_packs: bool = False

    #: Where the generated token is written (``<state_dir>/token``). The client library finds it
    #: by walking up from the notebook's directory.
    state_dir: Path = Path(".maprt_emulator")

    # -- behaviour ----------------------------------------------------------------------------
    #: Features to report as unlicensed; any of :data:`LICENSABLE`.
    unlicensed: tuple[str, ...] = ()

    #: Artificial delay on envelope endpoints, so a notebook can see that calls are not free.
    latency_ms: int = 0

    # -- the collision map --------------------------------------------------------------------
    #: ``auto`` prefers a recorded map and falls back to the model; ``recorded`` refuses to
    #: invent one; ``synthetic`` ignores the recordings. Pin it for a demo so you know what you
    #: are showing.
    map_source: Literal["auto", "recorded", "synthetic"] = "auto"

    #: Varies the small per-surface margin in the clearance model. Same seed, same maps.
    seed: int = 0

    #: Roughly how long GetMap takes to stream its response. A real appliance takes minutes
    #: (§4.2.1.3); a few seconds is enough for a notebook to see that the body arrives
    #: progressively, without making the workshop tedious.
    map_seconds: float = 3.0

    #: The fault armed for the whole run (:mod:`maprt_emulator.scenarios`); ``ok`` arms none.
    scenario: str = "ok"

    #: Randomise the order of the map's triples. §5.2.6 says the list is not ordered, so this
    #: turns "you must not rely on the order" from an assertion into something demonstrable.
    shuffle_map: bool = False

    @field_validator("scenario")
    @classmethod
    def _known_scenario(cls, value):
        if value not in SCENARIOS:
            raise ValueError(f"no scenario {value!r}; choose from {', '.join(SCENARIOS)}")
        return value

    @property
    def token_path(self):
        return self.state_dir / "token"

    @property
    def server_path(self):
        return self.state_dir / "server.json"

    @property
    def base_url(self):
        return f"http://{self.host}:{self.port}"

    def resolve_token(self):
        """The token to use, generating and recording one when none was supplied.

        A fresh ``secrets.token_urlsafe(32)`` per start is the point: a workshop token is
        disposable, and nobody should end up with a long-lived shared secret in their notes.
        """
        if self.token:
            return self.token
        self.token = secrets.token_urlsafe(32)
        return self.token

    def write_token(self, packs=None):
        """Record where this server is and how to authenticate with it.

        Two files, both in :attr:`state_dir`:

        * ``token`` — the bare token, for ``cat``-ing or pasting.
        * ``server.json`` — the **URL as well**, which is what makes a non-default port work.
          Port 8000 is often taken, so a student starts the emulator with ``--port 8001`` and
          then every notebook fails against ``http://127.0.0.1:8000`` with nothing to suggest
          why. The server knows where it is; it should say so rather than leave the notebook
          guessing. With ``packs`` (:meth:`maprt_emulator.library.PackLibrary.report`, or
          ``{"off": True}`` for ``no_packs``), also what's served from packs and what was skipped.

        0600 on both, because one of them is a credential. It is a throwaway credential for a
        fake appliance, but a workshop that handles the disposable one sloppily teaches handling
        the real one sloppily.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self._write_private(self.token_path, self.resolve_token() + "\n")
        self._write_private(self.server_path, json.dumps({
            "url": self.base_url,
            "token": self.resolve_token(),
            "pid": os.getpid(),
            "started": dt.datetime.now().isoformat(timespec="seconds"),  # noqa: DTZ005 — local, for a human
            **({"packs": packs} if packs is not None else {}),
        }, indent=2) + "\n")
        return self.token_path

    @staticmethod
    def _write_private(path, text):
        path.write_text(text, encoding="utf-8")
        try:
            os.chmod(path, 0o600)  # audit: allow file-protection - best effort; Windows ignores it and the comment says so
        except OSError:
            pass  # Windows and some network filesystems; the write itself still succeeded.

    def is_licensed(self, feature):
        return feature not in self.unlicensed

    def as_env(self):
        """Every field as a ``{"MAPRT_EMULATOR_FIELD": "value"}`` mapping. **No side effects.**

        Only the ``--reload`` path needs this: reloading re-imports the app in a child process,
        so the settings have to survive the process boundary, and the environment is the only
        channel. Every field is therefore kept environment-expressible — scalars, or sequences
        that pydantic-settings reads back as JSON.

        It returns the mapping rather than writing it, so the caller decides when the process
        environment changes. An earlier version mutated ``os.environ`` directly and the test
        exercising it leaked settings into every later test and into subprocesses they started.
        A function whose whole job is to describe configuration should not reconfigure the
        process as a side effect of being asked.

        Not used on the normal path, where :func:`maprt_emulator.cli.main` builds the app itself and
        hands uvicorn the object. Going through the environment there would silently drop any
        field this method forgot, which is exactly the bug it exists to avoid.
        """
        exported = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, bool):
                # str(False) is "False", which pydantic-settings reads as truthy. Be explicit.
                text = "true" if value else "false"
            elif isinstance(value, (list, tuple)):
                text = json.dumps(list(value))
            else:
                text = str(value)
            exported[f"MAPRT_EMULATOR_{name.upper()}"] = text
        return exported
