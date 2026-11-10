"""Authentication: the token and the User-Agent, rejected the way the appliance rejects them.

Every rejection is HTTP 200 with ``success: false`` (recorded 2026-10-23), so a client that trusts
``raise_for_status()`` sails straight through all of them.
"""

import json

import pytest


class TestValidToken:
    def test_a_good_token_is_accepted(self, client, envelope_of):
        response = client.get("/integration/ping")
        assert response.status_code == 200
        assert envelope_of(response)["success"] is True


class TestWrongToken:
    def test_is_reported_at_http_200_by_default(self, client_factory, envelope_of):
        # The headline lesson. 200 OK, and the call did not succeed.
        with client_factory(token="not-the-token") as client:
            response = client.get("/integration/ping")
        assert response.status_code == 200
        body = envelope_of(response)
        assert body["success"] is False
        assert body["errorCode"] == "InvalidClientToken"
        assert body["errorCodeType"] == "AuthErrorCodes"

    def test_auth_401_switches_to_an_http_status(self, client_factory, envelope_of):
        with client_factory(token="not-the-token", auth_401=True) as client:
            response = client.get("/integration/ping")
        assert response.status_code == 401
        assert envelope_of(response)["errorCode"] == "InvalidClientToken"


class TestMissingOrMalformedCredential:
    """Recorded (2026-10-23): every token problem is a 200 + InvalidClientToken, never a 401."""

    @pytest.mark.parametrize("headers", [
        {},                                           # no Authorization header at all
        {"Authorization": "Bearer"},                  # the scheme with nothing after it
        {"Authorization": "Bearer   "},
    ], ids=["no-header", "empty-bearer", "blank-bearer"])
    def test_is_a_200_with_invalid_client_token(self, client_factory, envelope_of, headers):
        with client_factory(token=None) as client:
            response = client.get("/integration/ping", headers=headers)
        assert response.status_code == 200
        body = envelope_of(response)
        assert (body["success"], body["errorCode"], body["errorCodeType"]) \
            == (False, "InvalidClientToken", "AuthErrorCodes")
        assert body["errorData"] is None and "data" not in body

    @pytest.mark.parametrize("scheme", ["", "Basic "], ids=["bare-token", "basic"])
    def test_the_right_token_without_bearer_is_still_rejected(self, client_factory, envelope_of,
                                                              token, scheme):
        with client_factory(token=None) as client:
            body = envelope_of(client.get("/integration/ping",
                                          headers={"Authorization": f"{scheme}{token}"}))
        assert body["errorCode"] == "InvalidClientToken"

    def test_auth_401_applies_to_a_missing_header_too(self, client_factory, envelope_of):
        with client_factory(token=None, auth_401=True) as client:
            response = client.get("/integration/ping")
        assert response.status_code == 401
        assert envelope_of(response)["errorCode"] == "InvalidClientToken"


class TestEveryEndpointIsProtected:
    def test_no_endpoint_answers_without_a_token(self, client_factory, envelope_of):
        # A route added without the dependency would otherwise be silently open.
        paths = ["/integration/ping", "/integration/rooms", "/integration/rooms/TrueBeam2",
                 "/integration/patients/ANON001/surfaces",
                 "/integration/surfaces/dad7394f-6893-4ce3-96ed-8bc3091cf4c3"]
        with client_factory(token="wrong") as client:
            for path in paths:
                body = envelope_of(client.get(path))
                assert body["success"] is False, f"{path} answered without a valid token"
                assert body["errorCode"] == "InvalidClientToken", path


class TestUserAgent:
    """Recorded (2026-10-23): a User-Agent the appliance won't take is a 200 + VersionMismatch.

    It must be ``VisionRT.Integration.Saturn/`` and a version whose major.minor is not newer than
    the API's 2.0; the patch is ignored.
    """

    @pytest.mark.parametrize("agent", [
        "VisionRT.Integration.Saturn/2.0.0", "VisionRT.Integration.Saturn/2.0.1",
        "VisionRT.Integration.Saturn/1.2.8", "VisionRT.Integration.Saturn/2.0.my-script",
    ])
    def test_these_pass(self, client_factory, envelope_of, agent):
        with client_factory(user_agent=agent) as client:
            assert envelope_of(client.get("/integration/ping"))["success"] is True

    @pytest.mark.parametrize("agent", [
        "VisionRT.Integration.Saturn/2.1.0", "VisionRT.Integration.Saturn/3.0.0",
        "VisionRT.Integration.Saturn", "VisionRT.Integration.Saturn/", "curl/8.7.1", None,
    ])
    def test_these_get_version_mismatch(self, client_factory, envelope_of, agent):
        with client_factory(user_agent=agent) as client:
            response = client.get("/integration/ping")
        assert response.status_code == 200
        body = envelope_of(response)
        assert (body["success"], body["errorCode"], body["errorCodeType"], body["errorData"]) \
            == (False, "VersionMismatch", "AuthErrorCodes", None)

    def test_the_token_is_checked_first(self, client_factory, envelope_of):
        with client_factory(token="not-the-token", user_agent="curl/8.7.1") as client:
            assert envelope_of(client.get("/integration/ping"))["errorCode"] == "InvalidClientToken"

    def test_lenient_mode_only_logs(self, client_factory, envelope_of, caplog):
        # For a first lesson's bare GET, before the header has been explained.
        with client_factory(user_agent="curl/8.7.1", strict_user_agent=False) as client:
            assert envelope_of(client.get("/integration/ping"))["success"] is True
        assert "VersionMismatch" in caplog.text


class TestOpenPing:
    def test_serves_ping_without_a_token_when_asked(self, client_factory, envelope_of):
        with client_factory(token=None, open_ping=True) as client:
            assert envelope_of(client.get("/integration/ping"))["success"] is True

    def test_but_leaves_the_other_endpoints_protected(self, client_factory):
        with client_factory(token=None, open_ping=True) as client:
            assert json.loads(client.get("/integration/rooms").text)["errorCode"] == "InvalidClientToken"
