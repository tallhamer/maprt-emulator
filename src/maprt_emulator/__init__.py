"""maprt_emulator — the MapRT Emulator, a local emulator of the Vision RT MapRT API 2.0.

Serves the seven documented REST endpoints over FastAPI, with the same Bearer-token auth, the
same JSON envelope, the same two-ways-to-fail error model and the same streaming collision-map
wire format as a real appliance — reading surfaces and recorded clearance maps out of a folder,
and synthesizing plausible maps when no recording matches.

**Not a medical device. Teaching use only. Not affiliated with or endorsed by Vision RT.**
"""

__version__ = "0.1.0"
