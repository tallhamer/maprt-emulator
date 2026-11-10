"""GetBeamDeliveryStatus: bodies, errors, edges.

The agreement with a client-side map check lives in `test_beam_agreement.py`.
"""


import pytest

SURFACE = "a6f3fbee-6714-4f92-80c1-f5aadda4cbeb"
ROOM = "96bbc235-0da9-e983-0783-5973a7d11144"


def body(beam_list=None, **overrides):
    out = {"PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "PatientPosition": "HFS",
           "CouchBuffer": 20.0, "PatientBuffer": 20.0,
           "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0},
           "BeamList": beam_list if beam_list is not None else [
               {"beamNumber": 1,
                "controlPoints": [{"gantryAngle": 0, "couchAngle": 0,
                                   "coordinateSystem": "IEC_61217"}]}]}
    out.update(overrides)
    return out


@pytest.fixture
def api(shared_client):
    """One app for the module — these tests only read. The cases that need different settings
    (unlicensed features) build their own client from `client_factory` instead."""
    return shared_client


class TestTheSpecsBodyShape:
    def test_a_numeric_position_and_string_isocenter_are_accepted(self, api, envelope_of):
        # The spec's own example body uses the numeric PatientPosition and quotes the isocenter
        # components as strings; MapApp sends the DICOM string and numbers. Both must work.
        spec_style_body = {
            "PatientPosition": 1, "CouchBuffer": 20, "PatientBuffer": 20,
            "PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM,
            "Isocenter": {"x": "12.5", "y": "-40.25", "z": "101.75"},
            "BeamList": [
                {"beamNumber": 1, "controlPoints": [
                    {"gantryAngle": 0, "couchAngle": 0, "coordinateSystem": "IEC_61217"}]},
                {"beamNumber": 2, "controlPoints": [
                    {"gantryAngle": g, "couchAngle": 0, "coordinateSystem": "IEC_61217"}
                    for g in (10, 20, 30)]},
            ],
        }
        statuses = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                        json=spec_style_body))["data"]["statuses"]
        assert [s["beamNumber"] for s in statuses] == [1, 2]


class TestVerdictLogic:
    def test_a_beam_is_unsafe_if_any_control_point_collides(self, api, envelope_of):
        # One bad angle among good ones condemns the beam — "checks clearance for individual
        # control points" means all of them have to pass.
        clear = {"gantryAngle": 0, "couchAngle": 0, "coordinateSystem": "IEC_61217"}
        blocked = {"gantryAngle": 90, "couchAngle": 90, "coordinateSystem": "IEC_61217"}
        only_clear = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                          json=body([{"beamNumber": 1,
                                                      "controlPoints": [clear, clear]}])))
        mixed = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                     json=body([{"beamNumber": 1,
                                                 "controlPoints": [clear, blocked, clear]}])))
        assert only_clear["data"]["statuses"][0]["isSafe"] is True
        assert mixed["data"]["statuses"][0]["isSafe"] is False

    def test_a_beam_with_no_control_points_is_not_safe(self, api, envelope_of):
        # No evidence of safety is not evidence of safety. Reporting True for an empty beam
        # would be the most dangerous available default.
        statuses = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                        json=body([{"beamNumber": 1,
                                                    "controlPoints": []}])))["data"]["statuses"]
        assert statuses[0]["isSafe"] is False

    def test_an_empty_beam_list_gives_an_empty_statuses_list(self, api, envelope_of):
        # A success carrying nothing, not an error: you asked about no beams.
        result = envelope_of(api.post("/integration/GetBeamDeliveryStatus", json=body([])))
        assert result["success"] is True
        assert result["data"]["statuses"] == []

    def test_every_beam_gets_exactly_one_status(self, api, envelope_of):
        beams = [{"beamNumber": n, "controlPoints": [
            {"gantryAngle": n * 30, "couchAngle": 0, "coordinateSystem": "IEC_61217"}]}
            for n in range(1, 8)]
        statuses = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                        json=body(beams)))["data"]["statuses"]
        assert [s["beamNumber"] for s in statuses] == list(range(1, 8))


class TestErrors:
    def test_an_unknown_surface_is_an_error_at_http_200(self, api, envelope_of):
        response = api.post("/integration/GetBeamDeliveryStatus",
                            json=body(PatientSurfaceId="nope"))
        assert response.status_code == 200
        assert envelope_of(response)["errorCode"] == "SurfaceNotFound"

    def test_an_unknown_room_is_an_error(self, api, envelope_of):
        response = api.post("/integration/GetBeamDeliveryStatus", json=body(TreatmentRoomId="x"))
        assert envelope_of(response)["errorCode"] == "TreatmentRoomNotFound"

    def test_an_unlicensed_server_withholds_the_verdicts(self, client_factory, envelope_of):
        # Separately licensed (§2.3.3). "I am not licensed to tell you" must be distinguishable
        # from "these beams are unsafe".
        with client_factory(map_seconds=0.0, unlicensed=("beams",)) as client:
            response = client.post("/integration/GetBeamDeliveryStatus", json=body())
        assert envelope_of(response)["errorCode"] == "FeatureNotLicensed"

    def test_the_client_parser_raises_the_beam_specific_error(self, client_factory):
        # MapApp's parser raises BeamDeliveryStatusError here, a subclass, so an application can
        # route the withheld-verdict case separately while generic handling still matches.
        from client_wire import BeamDeliveryStatusError, MapRTApiError, parse_beam_delivery_status
        with client_factory(map_seconds=0.0, unlicensed=("beams",)) as client:
            text = client.post("/integration/GetBeamDeliveryStatus", json=body()).text
        with pytest.raises(BeamDeliveryStatusError) as exc:
            parse_beam_delivery_status(text)
        assert isinstance(exc.value, MapRTApiError)
        assert exc.value.category == "license"

    def test_a_non_iec_coordinate_system_is_refused_rather_than_guessed(self, api, envelope_of, caplog):
        # The API supports six couch coordinate systems; the conversions are vendor-specific and
        # are not implemented here. Treating a Native angle as IEC would give a confident wrong
        # answer about whether a beam is deliverable.
        with caplog.at_level("WARNING", logger="maprt_emulator"):
            response = api.post("/integration/GetBeamDeliveryStatus", json=body([
                {"beamNumber": 1, "controlPoints": [
                    {"gantryAngle": 0, "couchAngle": 0, "coordinateSystem": "Native"}]}]))
        assert response.status_code == 400
        assert envelope_of(response)["errorData"] is None
        assert "IEC_61217" in caplog.text

    def test_a_missing_control_point_angle_is_a_400(self, api):
        response = api.post("/integration/GetBeamDeliveryStatus", json=body([
            {"beamNumber": 1, "controlPoints": [{"gantryAngle": 0}]}]))
        assert response.status_code == 400

    def test_there_is_no_high_resolution_flag_to_set(self, api, envelope_of):
        # Spec §4.6.2: GetBeamDeliveryStatus has no HighResolution — nothing is being sampled.
        # An extra key is ignored rather than rejected, as the appliance ignores extras.
        result = envelope_of(api.post("/integration/GetBeamDeliveryStatus",
                                      json=body(HighResolution=True)))
        assert result["success"] is True
