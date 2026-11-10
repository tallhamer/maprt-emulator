# Potential additions (parked)

The emulator serves **only the MapRT REST API**: the seven `/integration` routes a real
appliance has, plus FastAPI's own `/docs`, `/redoc` and `/openapi.json`. Everything below was removed for that reason (or is planned and not built). It
is kept here so a consumer such as the SGRT Scripting Workshop can bring an item back as a
clearly labelled extension, outside `/integration`, if a lesson needs it.

The removed items came from the SGRT Scripting Workshop's emulator at commit `ea76921`; the file
named in each row is where the original code is.

| Addition | What it did, or would do | Original / status |
|---|---|---|
| `POST /integration/GetMapAndBeamTransitions` | A clearance map plus a safe/unsafe verdict for each move between consecutive beams. The MapRT client library has this method, but the REST API has no route for it, so both the path and the response shape were invented. | `src/MapRT/maprt_emulator/routes/transitions.py`, `mapgen.transition_verdicts`, `schemas.GetMapAndBeamTransitionsRequest`; used by workshop notebook 06 |
| `GET /emulator/state` | The armed scenario, the data being served, the settings in force, and which error codes were invented. Unauthenticated. | `routes/emulator.py`; polled by the workshop's `binder/start` for readiness, read by notebooks 02 and 07 |
| `GET /emulator/scenarios` | Every armable fault and what it teaches. | `routes/emulator.py`; now `maprt-emulator --list-scenarios` |
| `POST /emulator/scenario` | Arm a fault on a running server. | `routes/emulator.py`; now `--scenario NAME` (or `MAPRT_EMULATOR_SCENARIO`) at launch |
| **Inventory endpoint** (planned) | Lists every loaded pack, patient, surface and room, and the **recorded GetMap parameters** for each, so a user can call GetMap with supported parameters and get real data back. | **Not a route:** to be built as `maprt-emulator inventory` and an `inventory` block in `.maprt_emulator/server.json`, once the emulator serves session packs |

## What replaces them in the emulator itself

- **Readiness:** an authenticated `GET /integration/ping`.
- **Scenarios:** chosen when the emulator starts (`--scenario`, `MAPRT_EMULATOR_SCENARIO`) and
  listed with `--list-scenarios`.
- **What is being served:** printed in the start banner. Once the emulator serves session packs,
  the loaded packs will also be listed in `.maprt_emulator/server.json`.
- **Which source answered:** the server log, always. The `X-Emulator-Map-Source` and
  `X-Emulator-Beam-Source` response headers only with `--source-headers`, because an appliance
  sends no such header and a client must not come to look for one.
