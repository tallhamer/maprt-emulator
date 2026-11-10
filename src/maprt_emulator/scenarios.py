"""Fault injection: making every error branch reproducible on demand.

A client's error handling is the part that never gets tested, because the failures it exists for
are the ones you cannot arrange. An unlicensed feature needs an appliance without the licence; a
throttled server needs a busy one; a timeout needs patience. So they get written once, guessed
at, and never exercised — and then they are what fails on a Friday afternoon.

A scenario is chosen when the emulator starts, and stays armed until it stops::

    maprt-emulator --scenario unlicensed-map
    maprt-emulator --list-scenarios               # what each one does, and what it teaches

There is no HTTP route to arm one: a real appliance has no such route, and the emulator serves
only the MapRT API. Restart with another ``--scenario`` (or none, which is ``ok``) to change it.
"""

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("maprt_emulator.scenarios")


@dataclass(frozen=True)
class Scenario:
    """One armable fault. Every field is off by default, so ``ok`` is the empty scenario."""

    name: str
    description: str
    teaches: str = ""

    #: Features to report as unlicensed, on top of whatever ``--unlicensed`` already set.
    unlicensed: tuple = ()
    #: Reject every token as ``InvalidClientToken``, however correct it is.
    reject_token: bool = False
    #: Force this HTTP status on ``/integration`` requests instead of handling them.
    forced_status: int = 0
    #: Extra seconds before answering, for provoking client timeouts.
    extra_delay: float = 0.0
    #: Fail this many ``/integration`` requests with 503 + ``Retry-After``, then recover.
    throttle_failures: int = 0
    #: Cut the map stream off before its ``DONE`` line.
    truncate_map: bool = False
    #: Serve the map's triples in random order.
    shuffle_map: bool = False


SCENARIOS = {s.name: s for s in [
    Scenario(
        "ok",
        "Normal operation.",
        "Nothing. Arm this to clear whatever else is armed.",
    ),
    Scenario(
        "unlicensed-surfaces",
        "GET /integration/surfaces/{id} reports FeatureNotLicensed.",
        "Surface data is separately licensed (spec §2.3.3). Listing a patient's surfaces still "
        "works — it is fetching the mesh that is licensed — so a client has to handle being "
        "told about a surface it cannot have.",
        unlicensed=("surfaces",),
    ),
    Scenario(
        "unlicensed-map",
        "POST /integration/GetMap reports FeatureNotLicensed.",
        "That an HTTP 200 can carry a refusal. The map is the expensive licensed feature, and a "
        "client that only checks the status code will try to parse the error envelope as a map.",
        unlicensed=("map",),
    ),
    Scenario(
        "unlicensed-beams",
        "POST /integration/GetBeamDeliveryStatus reports FeatureNotLicensed.",
        "A withheld verdict is not a failed one. 'I am not licensed to tell you whether this "
        "beam is safe' must not be displayed as 'this beam is unsafe' — or as a crash.",
        unlicensed=("beams",),
    ),
    Scenario(
        "bad-token",
        "Every request is rejected as InvalidClientToken, whatever token is sent.",
        "What an expired or rotated token looks like from the client side. Note the status: by "
        "default a rejected token is an HTTP 200 with success:false (§5.3).",
        reject_token=True,
    ),
    Scenario(
        "server-error",
        "Every /integration request returns HTTP 500.",
        "The other half of the error model: a 5xx is a transport-level failure with no envelope "
        "to read, so a client that assumes it can always parse JSON will fail inside its own "
        "error handler.",
        forced_status=500,
    ),
    Scenario(
        "slow",
        "Every /integration request takes an extra 8 seconds.",
        "Timeouts. A real GetMap takes minutes (§4.2.1.3) and the documented client default is "
        "180 seconds — a client with a 5-second timeout decides a working appliance is broken.",
        extra_delay=8.0,
    ),
    Scenario(
        "throttle",
        "The next 2 /integration requests return HTTP 503 with Retry-After, then recovery.",
        "Retry with backoff. A server is allowed to throttle: to say it cannot take a request "
        "now and that the client should try again (§2.2.1.3). It recovers after 2 failures, so correct backoff code visibly succeeds "
        "instead of looping forever.",
        throttle_failures=2,
    ),
    Scenario(
        "truncate-map",
        "GetMap stops streaming before its DONE line.",
        "Why DONE exists. A truncated map parses into a plausible-looking array with missing "
        "angles silently left as collisions — so a client has to check for the terminator "
        "rather than trust whatever arrived.",
        truncate_map=True,
    ),
    Scenario(
        "shuffle-map",
        "GetMap serves its triples in random order.",
        "That §5.2.6 means it when it says 'the list is not ordered'. Each triple carries its "
        "own couch and gantry angle precisely so order cannot matter; code that indexes by "
        "arrival position breaks here and nowhere else.",
        shuffle_map=True,
    ),
]}

DEFAULT_SCENARIO = SCENARIOS["ok"]


@dataclass
class ScenarioState:
    """The armed scenario, and the questions the routes ask of it.

    Held on ``app.state.scenarios``, armed from ``settings.scenario`` when the app is built.
    Mutable only in the throttle countdown, which is what lets a correct retry loop succeed.
    """

    settings: object
    armed: Scenario = field(default=DEFAULT_SCENARIO)
    _throttle_remaining: int = 0

    def __post_init__(self):
        self.arm(getattr(self.settings, "scenario", DEFAULT_SCENARIO.name))

    def arm(self, name):
        """Arm the named scenario and return it. Raises :class:`KeyError` if there is no such one."""
        scenario = SCENARIOS[name]
        self.armed = scenario
        self._throttle_remaining = scenario.throttle_failures
        logger.info("Scenario armed: %s", name)
        return scenario

    # -- what the routes ask ------------------------------------------------------------------

    def is_licensed(self, feature):
        """Whether ``feature`` is licensed, honouring both ``--unlicensed`` and the scenario."""
        return self.settings.is_licensed(feature) and feature not in self.armed.unlicensed

    @property
    def rejects_token(self):
        return self.armed.reject_token

    @property
    def forced_status(self):
        return self.armed.forced_status or None

    @property
    def extra_delay(self):
        return self.armed.extra_delay

    @property
    def truncates_map(self):
        return self.armed.truncate_map

    @property
    def shuffles_map(self):
        """Honours ``--shuffle-map`` as well as the scenario."""
        return self.armed.shuffle_map or self.settings.shuffle_map

    def take_throttle_slot(self):
        """True if this request should be throttled; counts down so a retry eventually succeeds.

        Consuming the counter is what makes correct backoff code demonstrably correct: a server
        that throttled forever would teach students to write a retry loop that can never exit.
        """
        if self._throttle_remaining <= 0:
            return False
        self._throttle_remaining -= 1
        return True

    def describe(self):
        """The armed scenario as JSON."""
        return {
            "name": self.armed.name,
            "description": self.armed.description,
            "teaches": self.armed.teaches,
            "throttleFailuresRemaining": self._throttle_remaining,
        }


def catalogue():
    """Every scenario as JSON, for ``--list-scenarios``."""
    return [
        {
            "name": s.name,
            "description": s.description,
            "teaches": s.teaches,
            "effects": {
                "unlicensed": list(s.unlicensed),
                "rejectToken": s.reject_token,
                "forcedStatus": s.forced_status or None,
                "extraDelaySeconds": s.extra_delay,
                "throttleFailures": s.throttle_failures,
                "truncateMap": s.truncate_map,
                "shuffleMap": s.shuffle_map,
            },
        }
        for s in SCENARIOS.values()
    ]
