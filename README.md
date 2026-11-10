# maprt-emulator

A local emulator of the Vision RT **MapRT API 2.0** REST endpoints, for teaching and for testing
MapRT clients without an appliance.

> **Not a medical device. Teaching and testing use only.** Not affiliated with or endorsed by
> Vision RT.

It serves the seven MapRT REST routes:

| Method | Path |
|---|---|
| GET | `/integration/ping` |
| GET | `/integration/patients/{patientId}/surfaces` |
| GET | `/integration/surfaces/{surfaceId}` |
| GET | `/integration/rooms/{machineName}` |
| GET | `/integration/rooms` |
| POST | `/integration/GetMap` |
| POST | `/integration/GetBeamDeliveryStatus` |

It also has FastAPI's own pages, `/docs`, `/redoc` and `/openapi.json`, as any FastAPI
application does. A real appliance publishes no such pages, so a client must not depend on them.
They describe only what an appliance takes and sends. To try a call from `/docs`, **Authorize**
with the printed token and start the emulator with `--lenient-user-agent`: a browser can't send
the Saturn User-Agent, so a strict emulator answers every browser call with `VersionMismatch`.

## Run it

```console
pip install maprt-emulator
maprt-emulator                 # or: python -m maprt_emulator
```

(From a clone of this repository, `pip install .` installs it from the source instead.)

It listens on `http://127.0.0.1:8000`, mints a fresh API token at every start, prints it, and
writes it to `.maprt_emulator/token` and `.maprt_emulator/server.json`. Every setting can also be
pinned with a `MAPRT_EMULATOR_*` environment variable (`MAPRT_EMULATOR_TOKEN`,
`MAPRT_EMULATOR_PORT`, …); `maprt-emulator --help` lists the settings.

With no `--data-dir`, it serves the nearest `data/MapRT/` folder above the working directory, or
the small synthetic catalog built into the package.

## Serving recorded sessions (packs)

A **session pack** is a `.zip` of anonymized calls recorded against a real appliance
([`docs/PACK_FORMAT.md`](docs/PACK_FORMAT.md)). With packs loaded, the emulator answers from what
they recorded instead of the catalog:

```console
maprt-emulator --packs-dir packs/                 # every pack in the folder
maprt-emulator --packs-dir packs/ --pack hfs-capture --pack another   # only these, by name
maprt-emulator --packs-dir packs/ --check-data    # check them, and exit
maprt-emulator --no-packs                         # the catalog, even with MAPRT_EMULATOR_PACKS_DIR set
```

### Getting packs

Packs recorded on a real appliance are this repository's **release assets**: each release
`packs-v1`, `packs-v2`, … is one set, and its notes list every pack (what it holds, its size, its
sha256). Download only the ones you need with [`get_packs.py`](get_packs.py) (standard library
only; nothing is downloaded unless you name packs or ask for `--all`):

```console
python get_packs.py --release packs-v1 --list
python get_packs.py --release packs-v1 hfdr-phantom srs-patient    # into packs/
python get_packs.py --release packs-v1 --all
maprt-emulator --packs-dir packs
```

Pin a release (`--release packs-v1`, or a `…/download/packs-v1/…` URL) in anything that must not
change; never `…/latest/…`. The packs are CC BY 4.0 ([`LICENSE-packs`](LICENSE-packs)), not MIT.

### How packs are served

- **Every pack is checked when it loads.** A pack named with `--pack` that fails a check refuses
  startup; one found in the folder is skipped and reported, and if every one fails, startup is
  refused. Packs that disagree (one room GUID with different fields, say) always refuse startup.
  Each refusal says to use `--no-packs` to serve the catalog instead.
- **Recorded replies are served as recorded**, byte for byte. Requests are matched on the fields
  the API defines, so `20` matches `20.0` and key order doesn't matter.
- **Rooms** are every pack's recorded rooms; a lookup by machine name is answered from them.
- **A surface listed but never fetched** stays in the list, and fetching it answers
  `SurfaceNotFound`.
- **A GetMap that wasn't recorded** gets a map recorded for another request of the same resolution
  (real appliance output, not this request's data), else a computed one. **A beam check that
  wasn't recorded** is derived from the high-resolution map recorded for the same parameters, else
  computed.
- **The `Server` header and the ping version** are the newest appliance the packs were recorded on.

The banner, `.maprt_emulator/server.json` (its `packs` block) and the server log say what was
loaded, what was skipped and why, and which source answered each call.

**The inventory page** lists what's being served and what a call can reach: the packs (and any
skipped, with why), the rooms and what a lookup by machine name answers, each patient's surfaces,
and every recorded GetMap and beam check with a request body to copy that gets the recorded reply.
It's at `/inventory` (HTML) and `/inventory.md` (Markdown), with no token, and
`maprt-emulator inventory [the same options]` prints it without starting a server. Like `/docs`, it's
a page for people: a real appliance has none.

## How it answers

Like a real appliance, as recorded from one (Saturn/1.3.217.2, API 2.0.0):

- **A negative result is HTTP 200** with exactly four keys, `errorData: null`, `success: false`,
  an `errorCode` and an `errorCodeType`, and **no `data` key**: a token problem (`InvalidClientToken`), an unacceptable User-Agent
  (`VersionMismatch`), an unknown patient (`PatientNotFoundInDatabase`), surface
  (`SurfaceNotFound`) or room (`TreatmentRoomNotFound`), or an ambiguous machine name
  (`DuplicateKey`).
- **The User-Agent** must be `VisionRT.Integration.Saturn/<major>.<minor>.<patch>` no newer than
  2.0 (`--lenient-user-agent` only logs instead). The token is checked first.
- **GetMap** streams `text` with CRLF line endings and no `Content-Type`. Its errors are still
  the JSON envelope.
- Codes the emulator had to invent carry the type `EmulatorErrorCodes`, which no appliance sends.
  Their `errorData` is `null` too; the explanation is in the server log.
- **No extra headers.** Which source answered (a recorded or a computed map) is in the server
  log. `--source-headers` also sends it as `X-Emulator-Map-Source` / `X-Emulator-Beam-Source`
  (and `X-Emulator-Surface-Source` for a surface a pack listed but never fetched), which no
  appliance sends.

## Fault scenarios

`--scenario NAME` starts the emulator with a fault armed (an unlicensed feature, a rejected
token, a server error, a slow or throttled server, a truncated or shuffled map).
`--list-scenarios` describes each one.

## More

- [`docs/POTENTIAL_ADDITIONS.md`](docs/POTENTIAL_ADDITIONS.md): the non-API routes this emulator
  deliberately does not serve, and what replaces each one.
- [`docs/PACK_FORMAT.md`](docs/PACK_FORMAT.md): the session-pack format: what a pack holds, what
  its anonymization guarantees, and how the emulator checks and serves it.

## Changes

* **0.1.1:** the startup banner shows this session's API token on its own line, says whether it is
  new for this start or fixed (`--token`, `MAPRT_EMULATOR_TOKEN`), and how to read it safely: from
  the token file, never pasted into code or notebooks you share. "Run it" installs from PyPI.
* **0.1.0:** first release.

## Licences and citation

- **The code** (the emulator, `get_packs.py`): [MIT](LICENSE).
- **The packs** (the `packs-v*` release assets): [CC BY 4.0](LICENSE-packs). Cite them as:
  Tallhamer, M. J. (2026). *MapRT Emulator session packs* (packs-v1) [Data set]. AdventHealth.
  https://github.com/tallhamer/maprt-emulator

GitHub's "Cite this repository" button gives the emulator's citation, with the packs' in its
references ([`CITATION.cff`](CITATION.cff)). MapRT is a product of Vision RT Ltd; neither the
emulator nor the packs are made, endorsed or supported by Vision RT.
