"""Request bodies for the two POST endpoints, parsed leniently — as the appliance does.

The wire field names are PascalCase (``CouchBuffer``, ``HighResolution``) while the isocenter's
components are lowercase (``x``, ``y``, ``z``). Both are reproduced exactly, via aliases, so a
body that works here works against a real appliance.

**Lenient on purpose**, in the three places where real clients and the specification disagree:

* ``PatientPosition`` — spec §5.2.6 says send the *numeric* enum (``1`` for HFS). The
  live-verified MapApp client sends the **DICOM string** (``"HFS"``) and the appliance
  accepts it. Both are accepted here and normalised to the string.
* ``Isocenter`` components — the spec's own example quotes them as **strings**, while the
  client sends numbers. Both work.
* **Unknown keys are ignored.** The MapApp client puts a ``CoordinateSystem`` inside
  ``Isocenter``, which is not in the spec's example and which the appliance tolerates.

Being strict where the appliance is lenient would make the emulator reject requests that work in
the clinic, which is the opposite of useful.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Spec §4.5.6, the PatientPosition enumeration. Numeric value to DICOM patient-position string.
PATIENT_POSITION_BY_VALUE = {
    0: "HFP", 1: "HFS", 2: "HFDR", 3: "HFDL", 4: "FFDR", 5: "FFDL", 6: "FFP", 7: "FFS",
}
PATIENT_POSITION_NAMES = frozenset(PATIENT_POSITION_BY_VALUE.values())


class Isocenter(BaseModel):
    """The isocenter, in **mm**, in DICOM patient coordinates (spec §4.3.2)."""

    model_config = ConfigDict(extra="ignore")

    x: float
    y: float
    z: float

    def as_tuple(self):
        return (self.x, self.y, self.z)


class ClearanceRequest(BaseModel):
    """The common parameters of a clearance calculation (spec §4.6.1, ``ClearanceRequestBase``)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    #: Buffer around the couch surface, in **mm**. Note the unit: the MapApp client takes
    #: centimetres from the planner and multiplies by 10 before it gets here.
    couch_buffer: float = Field(0.0, alias="CouchBuffer")
    #: Buffer around the patient surface, in **mm**.
    patient_buffer: float = Field(0.0, alias="PatientBuffer")
    patient_position: str = Field("HFS", alias="PatientPosition")
    patient_surface_id: str = Field(..., alias="PatientSurfaceId")
    treatment_room_id: str = Field(..., alias="TreatmentRoomId")
    isocenter: Isocenter = Field(..., alias="Isocenter")

    @field_validator("patient_position", mode="before")
    @classmethod
    def _accept_number_or_name(cls, value):
        """Take either the spec's enum value or the DICOM string, and return the string."""
        if isinstance(value, bool):
            # ValueError, not TypeError: pydantic turns only ValueError into a validation error.
            raise ValueError("PatientPosition must be a position name or its enum value")  # noqa: TRY004
        if isinstance(value, int):
            try:
                return PATIENT_POSITION_BY_VALUE[value]
            except KeyError:
                raise ValueError(
                    f"PatientPosition {value} is not one of the enum values "
                    f"{sorted(PATIENT_POSITION_BY_VALUE)}"
                ) from None
        text = str(value).strip().upper()
        if text in PATIENT_POSITION_NAMES:
            return text
        # A numeric string ("1") is what a form-encoded or hand-written client tends to send.
        if text.isdigit() and int(text) in PATIENT_POSITION_BY_VALUE:
            return PATIENT_POSITION_BY_VALUE[int(text)]
        raise ValueError(
            f"PatientPosition {value!r} is not a DICOM patient position "
            f"({sorted(PATIENT_POSITION_NAMES)}) or an enum value "
            f"{sorted(PATIENT_POSITION_BY_VALUE)}"
        )

    @field_validator("couch_buffer", "patient_buffer")
    @classmethod
    def _buffers_are_not_negative(cls, value):
        # A negative buffer would shrink the patient, which is not a thing anyone means to ask
        # for and would make a map *more* permissive than reality. Refuse it.
        if value < 0:
            raise ValueError("buffers are in mm and cannot be negative")
        return value


class GetMapRequest(ClearanceRequest):
    """Spec §4.6.4 / §5.2.6.

    ``HighResolution`` true evaluates gantry and couch at 1 degree; false uses a coarser grid and
    is much faster. Both come back in the same wire format — but **not** as the same array shape,
    which catches people out: a client's parser has to infer the grid from the header line.
    """

    high_resolution: bool = Field(False, alias="HighResolution")


class ControlPoint(BaseModel):
    """One gantry/couch position a beam passes through (spec §4.5.5, ``GantryCouchPosition``)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    gantry_angle: float = Field(..., alias="gantryAngle")
    couch_angle: float = Field(..., alias="couchAngle")
    coordinate_system: str = Field("IEC_61217", alias="coordinateSystem")

    @field_validator("coordinate_system", mode="before")
    @classmethod
    def _only_iec_61217(cls, value):
        """Accept the IEC 61217 scale only, and say so rather than guessing.

        The API genuinely supports six couch coordinate systems (§4.3.1) and a real appliance
        converts between them — the conversion rules are vendor-specific and live in a separate
        Vision RT document. Quietly treating a ``Native`` angle as though it were IEC 61217 would
        produce confident, wrong verdicts about whether a beam is deliverable, so the emulator
        refuses instead of pretending.
        """
        text = str(value).strip() if value is not None else "IEC_61217"
        if text.upper() not in {"IEC_61217", "1"}:
            raise ValueError(
                f"coordinateSystem {value!r}: this emulator only implements IEC_61217. A real "
                f"appliance converts between the six systems in the specification; the "
                f"conversions are vendor-specific and are not reproduced here."
            )
        return "IEC_61217"


class Beam(BaseModel):
    """A beam as a list of control points (spec §4.5.1).

    ``beamNumber`` is an ``int`` in the specification. A string is coerced, because the
    MapApp client passes one (``beam_entry("1", …)``) and it works against the real
    appliance — but the response echoes an int, as §5.2.7's example does.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    beam_number: int = Field(..., alias="beamNumber")
    control_points: list[ControlPoint] = Field(default_factory=list, alias="controlPoints")


class GetBeamDeliveryStatusRequest(ClearanceRequest):
    """Spec §4.6.2 / §5.2.7. No ``HighResolution``: nothing is being sampled."""

    beam_list: list[Beam] = Field(default_factory=list, alias="BeamList")
