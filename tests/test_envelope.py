"""The envelope shape, and the two-ways-to-fail error model."""

import pytest

from maprt_emulator import envelope


class TestOk:
    def test_has_the_five_keys_a_client_reads(self):
        assert set(envelope.ok("x")) == {"data", "errorData", "success", "errorCode",
                                        "errorCodeType"}

    def test_success_true_and_no_error_code(self):
        body = envelope.ok([1, 2])
        assert body["success"] is True
        assert body["data"] == [1, 2]
        assert body["errorCode"] is None
        assert body["errorCodeType"] == "NoErrorCodes"


class TestFail:
    def test_has_the_recorded_four_keys_and_no_data(self):
        # Every recorded failure: no data key at all, and errorData null.
        body = envelope.fail(*envelope.INVALID_CLIENT_TOKEN)
        assert list(body) == ["errorData", "success", "errorCode", "errorCodeType"]
        assert body["errorData"] is None

    def test_success_false_carries_the_code(self):
        body = envelope.fail(*envelope.INVALID_CLIENT_TOKEN)
        assert body["success"] is False
        assert body["errorCode"] == "InvalidClientToken"
        assert body["errorCodeType"] == "AuthErrorCodes"


class TestMapRTFailure:
    def test_defaults_to_http_200_because_that_is_the_api_convention(self):
        # The headline fact about this API: a negative result is a 200. If this default ever
        # becomes 4xx, the emulator has stopped teaching the thing it exists to teach.
        assert envelope.MapRTFailure(envelope.FEATURE_NOT_LICENSED).status_code == 200

    def test_renders_to_an_envelope_without_its_detail(self):
        # The detail is for the server log; errorData stays null, as an appliance sends it.
        exc = envelope.MapRTFailure(envelope.DUPLICATE_KEY, detail="two rooms")
        body = exc.envelope()
        assert body["errorCode"] == "DuplicateKey"
        assert body["errorCodeType"] == "TreatmentRoomAdministrationErrorCodes"
        assert body["errorData"] is None
        assert exc.detail == "two rooms"

    def test_a_4xx_can_be_asked_for_explicitly(self):
        assert envelope.MapRTFailure(envelope.INVALID_CLIENT_TOKEN, status_code=401).status_code == 401


class TestInventedCodesAreDeclared:
    def test_the_codes_this_project_made_up_are_listed(self):
        # A code added to EMULATED without being listed here would quietly look authoritative —
        # which is why this test is written as an exact list and is *meant* to fail when a code
        # is added.
        assert envelope.INVENTED_CODES == ["InternalError", "InvalidArgument", "NoRecordedMap",
                                           "ServerBusy"]

    def test_every_invented_code_has_the_emulator_type_and_no_other_does(self):
        codes = [value for name, value in vars(envelope).items()
                 if name.isupper() and isinstance(value, tuple) and len(value) == 2]
        for code, code_type in codes:
            assert (code in envelope.INVENTED_CODES) == (code_type == envelope.EMULATED), code

    @pytest.mark.parametrize("code,expected", [
        (envelope.INVALID_CLIENT_TOKEN, ("InvalidClientToken", "AuthErrorCodes")),
        (envelope.VERSION_MISMATCH, ("VersionMismatch", "AuthErrorCodes")),
        (envelope.DUPLICATE_KEY, ("DuplicateKey", "TreatmentRoomAdministrationErrorCodes")),
        (envelope.PATIENT_NOT_FOUND, ("PatientNotFoundInDatabase", "PatientErrorCodes")),
        (envelope.SURFACE_NOT_FOUND, ("SurfaceNotFound", "PatientErrorCodes")),
        (envelope.ROOM_NOT_FOUND, ("TreatmentRoomNotFound", "PlanErrorCodes")),
    ])
    def test_the_recorded_codes_are_exactly_what_the_appliance_sent(self, code, expected):
        # Read from Session Recordings of a real appliance, 2026-10-23.
        assert code == expected

    def test_the_spec_quoted_codes_are_not_in_that_list(self):
        for code, _type in (envelope.INVALID_CLIENT_TOKEN, envelope.FEATURE_NOT_LICENSED,
                            envelope.DUPLICATE_KEY, envelope.VERSION_MISMATCH,
                            envelope.PATIENT_NOT_FOUND, envelope.SURFACE_NOT_FOUND,
                            envelope.ROOM_NOT_FOUND):
            assert code not in envelope.INVENTED_CODES
