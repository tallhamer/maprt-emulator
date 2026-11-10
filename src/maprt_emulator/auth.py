"""Bearer-token auth and the User-Agent check, as a real appliance applies them.

Three headers go on every MapRT API call::

    Content-Type:  application/json
    Authorization: Bearer <API_access_token>
    User-Agent:    VisionRT.Integration.Saturn/<major>.<minor>.<patch>

The token is a **shared secret** issued per appliance — not a login, not a session, nothing to
refresh. It is the whole of the authentication story, which is why a real deployment puts the
appliance behind TLS on a controlled network and treats the token like a password.

What a real appliance does, read from recordings of one (Saturn/1.3.217.2, API 2.0.0,
2026-10-23), and what the emulator therefore does:

* **Any token problem** — no ``Authorization`` header, ``Bearer`` with nothing after it, the token
  without the ``Bearer`` prefix, or a wrong token — gets HTTP **200** with ``success: false`` and
  ``InvalidClientToken``. Never a 401. (``--auth-401`` returns 401 instead, for exercising a
  client's other branch.)
* **The token is checked first.** A wrong token with a bad User-Agent is ``InvalidClientToken``.
* **The User-Agent** must be ``VisionRT.Integration.Saturn/`` followed by a version whose
  *major.minor* is not newer than the API's (2.0): ``…/2.0.0``, ``…/2.0.1`` and ``…/1.2.8``
  pass; ``…/2.1.0``, ``…/3.0.0``, ``…/`` with no version, no header at all, or ``curl/8.7.1``
  all get HTTP 200, ``success: false``, ``VersionMismatch``. The patch number is ignored.

The status code is the trap: a client that trusts it reads every one of these as a success.
"""

import logging

from fastapi import Request
from fastapi.security import HTTPBearer

from maprt_emulator import envelope
from maprt_emulator.settings import API_VERSION, USER_AGENT_PREFIX

logger = logging.getLogger("maprt_emulator.auth")


#: For ``/docs`` only: it gives the page an Authorize button that sends ``Bearer <token>``.
#: ``auto_error=False`` means it never answers a request itself; :func:`require_token` does,
#: with the envelope an appliance sends.
bearer_scheme = HTTPBearer(auto_error=False, description="The token the emulator printed at start.")


def bearer_token(request):
    """The token from the ``Authorization`` header, or ``None`` if none was presented."""
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _major_minor(version):
    """``(major, minor)`` from a ``a.b[.c…]`` version string, or ``None`` if it isn't one."""
    parts = version.split(".")
    if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
        return None
    return int(parts[0]), int(parts[1])


def user_agent_accepted(agent, api_version=API_VERSION):
    """Whether an appliance serving ``api_version`` accepts this User-Agent (see the module notes)."""
    if not agent or not agent.startswith(USER_AGENT_PREFIX):
        return False
    client = _major_minor(agent[len(USER_AGENT_PREFIX):])
    return client is not None and client <= _major_minor(api_version)


def check_user_agent(request, settings):
    """Refuse a User-Agent the appliance would refuse, unless ``--lenient-user-agent`` is set.

    Strict by default, because the appliance is. Lenient only logs, so a beginner's first bare
    ``GET`` can work before the header has been explained.
    """
    agent = request.headers.get("user-agent", "")
    if user_agent_accepted(agent):
        return
    if settings.strict_user_agent:
        raise envelope.MapRTFailure(envelope.VERSION_MISMATCH)
    logger.warning("User-Agent %r would get VersionMismatch from an appliance", agent or "(none)")


async def require_token(request: Request):
    """FastAPI dependency: authenticate the request, or raise :class:`envelope.MapRTFailure`.

    The token first, then the User-Agent — the appliance's order. Also the one place both are
    applied, so every authenticated endpoint gets them without each route remembering to ask.
    """
    settings = request.app.state.settings
    presented = bearer_token(request)

    # The `bad-token` scenario refuses a correct token too — what an expired or rotated secret
    # looks like from the client side, which is otherwise impossible to arrange on demand.
    rejected = (presented is None or presented != request.app.state.token
                or request.app.state.scenarios.rejects_token)
    if rejected:
        raise envelope.MapRTFailure(envelope.INVALID_CLIENT_TOKEN,
                                    status_code=401 if settings.auth_401 else 200)

    check_user_agent(request, settings)
    return presented
