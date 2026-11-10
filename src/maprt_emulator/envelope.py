"""The MapRT JSON response envelope, and the error codes the appliance reports.

Every endpoint but GetMap answers with an envelope. A success has five keys::

    {"data": …, "errorData": null, "success": true, "errorCode": null,
     "errorCodeType": "NoErrorCodes"}

and a negative result has four, with **no** ``data`` key, as every recorded failure has::

    {"errorData": null, "success": false, "errorCode": "…", "errorCodeType": "…"}

``errorData`` is always ``null``: no recorded error carried anything there, so the emulator's
own errors don't either. What went wrong goes to the server log instead, where someone debugging
sees it, and not into a body a client could come to rely on.

The thing to understand — and the reason this module exists rather than returning bare dicts
from each route — is that the MapRT API fails in **two different ways** (spec §5.3):

* A **malformed or rejected request** gets an HTTP **4xx**. 5xx means the server broke.
* A **negative result** — the request was fine, the answer is no — comes back at HTTP **200**
  with ``success: false`` and an ``errorCode``. Bad token, unlicensed feature, ambiguous room
  name: all of these are 200s.

So ``response.status_code == 200`` does not mean the call succeeded, and
``raise_for_status()`` alone will happily let an auth failure through.
"""

NO_ERROR = "NoErrorCodes"


def ok(data):
    """A successful envelope carrying ``data``."""
    return {
        "data": data,
        "errorData": None,
        "success": True,
        "errorCode": None,
        "errorCodeType": NO_ERROR,
    }


def fail(error_code, error_code_type):
    """A negative-result envelope. Served at HTTP 200 unless a route says otherwise."""
    return {
        "errorData": None,
        "success": False,
        "errorCode": error_code,
        "errorCodeType": error_code_type,
    }


# ---------------------------------------------------------------------------------------------
# Error codes.
#
# RECORDED: what a real appliance (Saturn/1.3.217.2, API 2.0.0) sent back for each case, read
# from Session Recordings made with MapApp and scripts/record_edge_cases.py (2026-10-23). Every
# one arrived at HTTP 200 with ``errorData: null``, so the emulator sends exactly that: no
# explanatory text a client could come to depend on.
#
# FROM THE SPEC: cases no recording could show (a feature switched off by licence).
#
# INVENTED: the emulator's own, for things only an emulator does (an armed fault, a refused
# recorded-map request) or that nothing has recorded yet (a malformed body). They carry the
# ``EmulatorErrorCodes`` type, which no appliance sends, so nobody mistakes one for a real code.
# ---------------------------------------------------------------------------------------------

AUTH = "AuthErrorCodes"
LICENSE = "LicenseErrorCodes"
ROOM_ADMIN = "TreatmentRoomAdministrationErrorCodes"
PATIENT = "PatientErrorCodes"
PLAN = "PlanErrorCodes"
#: Not a real MapRT error-code type. See the note above.
EMULATED = "EmulatorErrorCodes"

#: Recorded: a missing, malformed or wrong token, on every route.
INVALID_CLIENT_TOKEN = ("InvalidClientToken", AUTH)
#: Recorded: a missing or non-Saturn User-Agent, or one newer than the API version.
VERSION_MISMATCH = ("VersionMismatch", AUTH)
#: Recorded: ``GET /rooms/{machineName}`` matching more than one room.
DUPLICATE_KEY = ("DuplicateKey", ROOM_ADMIN)
#: Recorded: ``GET /patients/{id}/surfaces`` for a patient the appliance does not have.
PATIENT_NOT_FOUND = ("PatientNotFoundInDatabase", PATIENT)
#: Recorded: an unknown surface id, from ``GET /surfaces/{id}`` and from GetMap alike.
SURFACE_NOT_FOUND = ("SurfaceNotFound", PATIENT)
#: Recorded: an unknown treatment room id in GetMap. Note the type: a *plan* error.
ROOM_NOT_FOUND = ("TreatmentRoomNotFound", PLAN)
#: From the spec: a feature the appliance is not licensed for.
FEATURE_NOT_LICENSED = ("FeatureNotLicensed", LICENSE)
#: Invented — stands in for the server-side faults a ``--scenario`` can arm.
INTERNAL_ERROR = ("InternalError", EMULATED)
#: Invented — a malformed request body. No recording shows what an appliance sends.
INVALID_ARGUMENT = ("InvalidArgument", EMULATED)
#: Invented — the ``throttle`` scenario's 503.
SERVER_BUSY = ("ServerBusy", EMULATED)
#: Emulator-only: ``--map-source recorded`` is set and nothing matches. Has no analogue on a
#: real appliance, which always computes.
NO_RECORDED_MAP = ("NoRecordedMap", EMULATED)

#: Which of the above this project made up.
INVENTED_CODES = sorted({INTERNAL_ERROR[0], INVALID_ARGUMENT[0], SERVER_BUSY[0],
                         NO_RECORDED_MAP[0]})


class MapRTFailure(Exception):
    """A failure to render as a MapRT envelope.

    Raise it from anywhere in a route or dependency; the handler registered in
    :mod:`maprt_emulator.app` turns it into the envelope at ``status_code``. The default status is
    **200**, because that is how a real appliance reports a negative result — passing a 4xx is
    the deliberate exception, not the norm.

    ``detail`` explains the failure in the server log. It is never sent: an appliance's
    ``errorData`` is always ``null``.
    """

    def __init__(self, code, status_code=200, detail=None):
        self.error_code, self.error_code_type = code
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"{self.error_code_type}: {self.error_code} (HTTP {status_code})")

    def envelope(self):
        return fail(self.error_code, self.error_code_type)
