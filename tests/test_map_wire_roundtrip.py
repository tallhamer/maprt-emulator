"""What the emulator streams must parse with the real client's parser.

The emulator rests on this: `parse_collision_map` (copied into `client_wire` from MapApp's
`maprt_client.wire`) is the parser from a live MapRT integration, and the emulator is written to satisfy *it*, not the other way round. If
the emulator's wire format drifts, a notebook that works here stops meaning anything about the
clinic — so these tests take the emulator's own bytes and push them through the ported parser.

Also pinned here: the shapes. A 1-degree map parses to `(360, 181)`; the specification's coarse
`37,36` sampling parses to `(360, 185)`, because the couch axis spans 180 degrees *inclusive* and
no integer step above 1 both tiles it and fills 181 columns. See the parser's docstring. The two resolutions do not share a shape, and that is arithmetic, not a bug.
"""

import client_wire as wire
import numpy as np
import pytest

from maprt_emulator import mapgen
from maprt_emulator.mapgen import SurfaceModel, compute_map, map_text
from maprt_emulator.schemas import GetMapRequest
from maprt_emulator.settings import BUILTIN_DATA_DIR

# The display axes the parser lays a map out on.
GANTRY_ROWS = np.hstack((np.arange(180, 360), np.arange(0, 180)))
COUCH_COLS = np.hstack((np.arange(270, 360), np.arange(0, 91)))

SURFACE_ID = "dad7394f-6893-4ce3-96ed-8bc3091cf4c3"


@pytest.fixture(scope="module")
def surface():
    data = (BUILTIN_DATA_DIR / "surfaces" / f"{SURFACE_ID}.obj").read_bytes()
    return SurfaceModel.from_obj_bytes(data, SURFACE_ID)


def make_request(**overrides):
    body = {"PatientSurfaceId": SURFACE_ID, "TreatmentRoomId": "room",
            "PatientPosition": "HFS", "CouchBuffer": 20.0, "PatientBuffer": 20.0,
            "HighResolution": False, "Isocenter": {"x": 0.0, "y": 0.0, "z": 0.0}}
    iso = overrides.pop("isocenter", None)
    if iso is not None:
        body["Isocenter"] = {"x": iso[0], "y": iso[1], "z": iso[2]}
    body.update(overrides)
    return GetMapRequest.model_validate(body)


def computed(surface, **overrides):
    """``(body_text, parsed_image)`` for a request."""
    request = make_request(**overrides)
    couch, gantry, safe = compute_map(surface, request)
    text = map_text(couch, gantry, safe)
    image, _x, _y = wire.parse_collision_map(text)
    return text, image


@pytest.fixture(scope="module")
def default_high_res(surface):
    """The default high-resolution map, computed once — it costs about a second of CPU."""
    return computed(surface, HighResolution=True)


class TestTheWireFormat:
    def test_the_header_is_couch_samples_then_gantry_samples(self, surface):
        text, _ = computed(surface, HighResolution=True)
        assert text.split("\r\n", 1)[0] == "181,360"

    def test_the_coarse_header_matches_the_specs_own_example(self, surface):
        text, _ = computed(surface, HighResolution=False)
        assert text.split("\r\n", 1)[0] == "37,36"

    def test_the_body_ends_with_DONE(self, surface):
        text, _ = computed(surface)
        assert text.rstrip().endswith("DONE")

    def test_there_are_exactly_couch_times_gantry_triples(self, surface):
        text, _ = computed(surface)
        tokens = text.split()
        couch_samples, gantry_samples = (int(v) for v in tokens[0].split(","))
        # tokens are: header, one per triple, DONE
        assert len(tokens) - 2 == couch_samples * gantry_samples

    def test_every_triple_is_couch_gantry_flag_on_the_expected_scales(self, surface):
        text, _ = computed(surface)
        for token in text.split()[1:-1]:
            couch, gantry, flag = (int(v) for v in token.split(","))
            assert couch in COUCH_COLS, f"couch {couch} is off the IEC scale"
            assert 0 <= gantry <= 359, f"gantry {gantry} is off the IEC scale"
            assert flag in (0, 1), f"isOK was {flag}, not 0 or 1"

    def test_the_first_line_is_gantry_180_as_in_the_spec_example(self, surface):
        # Cosmetic — the spec says the list is unordered — but matching its example means a
        # client that wrongly depends on order still sees realistic data here.
        text, _ = computed(surface)
        first_triple = text.splitlines()[1]
        assert first_triple.split(",")[1] == "180"


class TestParsedShapes:
    def test_high_resolution_parses_to_360_by_181(self, surface):
        _, image = computed(surface, HighResolution=True)
        assert image.shape == (360, 181)

    def test_the_coarse_map_parses_to_360_by_185(self, surface):
        # Not 181. See the module docstring: inclusive axis, integer upsampling.
        _, image = computed(surface, HighResolution=False)
        assert image.shape == (360, 185)

    def test_both_resolutions_give_360_gantry_rows(self, surface):
        _, low = computed(surface, HighResolution=False)
        _, high = computed(surface, HighResolution=True)
        assert low.shape[0] == high.shape[0] == 360


class TestAngleAlignment:
    """The thing that actually has to be right: a given angle lands where the beam check looks.

    These start from an all-clear grid and block one angle, rather than from the model's output.
    An earlier version used the computed map and asserted that the row *next* to the blocked one
    was clear — which fails for gantry 90 and 270 because those sit inside the model's natural
    lateral collision bands. Alignment is a property of the wire format and the parser; mixing
    the model's content into it tests two things at once and gets a confusing answer.
    """

    @staticmethod
    def clear_grid(high_resolution):
        couch, gantry = mapgen.grid_for(high_resolution)
        return couch, gantry, np.ones((len(gantry), len(couch)), dtype=np.int8)

    @pytest.mark.parametrize("high_resolution", [True, False])
    @pytest.mark.parametrize("gantry", [180, 185, 190, 270, 350, 355, 0, 10, 90, 179])
    def test_a_blocked_gantry_angle_lands_on_its_display_row(self, gantry, high_resolution):
        couch_angles, gantry_angles, safe = self.clear_grid(high_resolution)
        if gantry not in gantry_angles:
            pytest.skip(f"gantry {gantry} is not sampled at this resolution")
        safe[gantry_angles.index(gantry), :] = 0

        image, _x, _y = wire.parse_collision_map(map_text(couch_angles, gantry_angles, safe))
        row = int(np.flatnonzero(GANTRY_ROWS == gantry)[0])
        # A coarse sample covers `step` degrees once upsampled, so the block is that tall.
        step = 1 if high_resolution else mapgen.LOW_RES_GANTRY_STEP
        assert image[row:row + step].max() == 0, f"gantry {gantry} is not at row {row}"
        # And nothing else is touched, which is what makes it an alignment test rather than a
        # "something somewhere is blocked" test.
        assert image.sum() == image.size - step * image.shape[1]

    @pytest.mark.parametrize("high_resolution", [True, False])
    @pytest.mark.parametrize("couch", [270, 275, 300, 355, 0, 5, 45, 90])
    def test_a_blocked_couch_angle_lands_on_its_display_column(self, couch, high_resolution):
        couch_angles, gantry_angles, safe = self.clear_grid(high_resolution)
        if couch not in couch_angles:
            pytest.skip(f"couch {couch} is not sampled at this resolution")
        safe[:, couch_angles.index(couch)] = 0

        image, _x, _y = wire.parse_collision_map(map_text(couch_angles, gantry_angles, safe))
        col = int(np.flatnonzero(COUCH_COLS == couch)[0])
        step = 1 if high_resolution else mapgen.LOW_RES_COUCH_STEP
        assert image[:, col:col + step].max() == 0, f"couch {couch} is not at column {col}"
        assert image.sum() == image.size - step * image.shape[0]

    def test_the_couch_overshoot_columns_are_a_repeat_of_couch_90(self):
        # The coarse map's 4 extra columns (185 vs 181). Pinning what is in them, because
        # "harmless trailing repeat" is a claim the parser's docstring makes on my behalf.
        couch_angles, gantry_angles, safe = self.clear_grid(high_resolution=False)
        safe[:, couch_angles.index(90)] = 0
        image, _x, _y = wire.parse_collision_map(map_text(couch_angles, gantry_angles, safe))
        assert image.shape[1] == 185
        assert image[:, 180:185].max() == 0      # couch 90's block runs to the end
        assert image[:, 179].min() == 1          # and stops there


class TestTheModelIsPhysicallySensible:
    """Not physics, but it has to behave the way a planner expects or it teaches the wrong thing."""

    def test_gantry_0_and_180_are_independent_of_the_couch_angle(self, default_high_res):
        # The source offset carries sin(gantry) on both couch-dependent axes, so at gantry 0 and
        # 180 the couch angle cannot affect anything. If this ever fails the geometry is wrong.
        _, image = default_high_res
        for gantry in (0, 180):
            row = image[int(np.flatnonzero(GANTRY_ROWS == gantry)[0])]
            assert row.min() == row.max(), f"gantry {gantry} varies with couch angle"

    def test_anterior_and_posterior_beams_are_clear(self, default_high_res):
        # Gantry 0 (from above) and 180 (through the couch) are routinely deliverable. A model
        # that blocks them — an earlier version did, by treating the couch as a half-space —
        # produces maps that are obviously wrong to anyone who has seen a real one.
        _, image = default_high_res
        for gantry in (0, 180):
            assert image[int(np.flatnonzero(GANTRY_ROWS == gantry)[0])].min() == 1

    def test_a_bigger_buffer_never_opens_up_an_angle(self, surface):
        # Monotonic: expanding the patient or the couch can only ever create collisions. This is
        # what makes a buffer sweep in a notebook interpretable.
        previous = None
        for buffer_mm in (0, 10, 20, 40, 80):
            _, image = computed(surface, HighResolution=True,
                                CouchBuffer=buffer_mm, PatientBuffer=buffer_mm)
            if previous is not None:
                opened = (image > previous).sum()
                assert opened == 0, f"{opened} angles became safe going to a {buffer_mm} mm buffer"
            previous = image

    def test_a_large_buffer_eventually_blocks_almost_everything(self, surface):
        _, image = computed(surface, HighResolution=True,
                            CouchBuffer=600, PatientBuffer=600)
        assert image.mean() < 0.02

    def test_a_default_request_is_mostly_clear(self, default_high_res):
        # A real clearance map is mostly deliverable with collision bands, not a coin flip.
        _, image = default_high_res
        assert 0.6 < image.mean() < 0.95

    def test_the_lateral_collision_bands_pinch_as_the_couch_is_kicked(self, default_high_res):
        # The bow-tie: at couch 0 the gantry has the most freedom, and kicking the couch takes it
        # away. This is the single most recognisable feature of a MapRT map.
        _, image = default_high_res
        clear_at_couch = {c: image[:, int(np.flatnonzero(COUCH_COLS == c)[0])].mean()
                          for c in (0, 30, 60, 90)}
        assert clear_at_couch[0] > clear_at_couch[30] > clear_at_couch[60] > clear_at_couch[90]

    def test_different_surfaces_give_different_maps(self, default_high_res):
        others = [p for p in sorted((BUILTIN_DATA_DIR / "surfaces").glob("*.obj"))
                  if p.stem != SURFACE_ID]
        _, mine = default_high_res
        for path in others:
            other = SurfaceModel.from_obj_bytes(path.read_bytes(), path.stem)
            _, theirs = computed(other, HighResolution=True)
            # Derived from the actual mesh, so two different bodies differ for the right reason.
            assert not np.array_equal(mine, theirs), f"{path.stem} produced an identical map"

    def test_moving_the_isocenter_changes_the_map(self, surface, default_high_res):
        _, centre = default_high_res
        _, shifted = computed(surface, HighResolution=True, isocenter=(0.0, 0.0, 600.0))
        assert not np.array_equal(centre, shifted)


class TestShuffling:
    def test_a_shuffled_body_parses_to_the_same_map(self, surface):
        # Spec §5.2.6: "The list is not ordered." The parser must not care, and --shuffle-map
        # exists so a notebook can prove it rather than take it on faith.
        request = make_request(HighResolution=False)
        couch, gantry, safe = compute_map(surface, request)
        ordered = map_text(couch, gantry, safe)
        shuffled = map_text(couch, gantry, safe, shuffle=True,
                            rng=np.random.default_rng(1))
        assert ordered != shuffled
        assert np.array_equal(wire.parse_collision_map(ordered)[0],
                              wire.parse_collision_map(shuffled)[0])

    def test_shuffling_keeps_the_header_and_the_DONE_marker_in_place(self, surface):
        request = make_request(HighResolution=False)
        couch, gantry, safe = compute_map(surface, request)
        text = map_text(couch, gantry, safe, shuffle=True, rng=np.random.default_rng(2))
        assert text.split("\r\n", 1)[0] == "37,36"
        assert text.rstrip().endswith("DONE")


class TestTheShippedRecording:
    def test_parses_and_has_the_shape_its_index_entry_claims(self):
        # The recorded map in the built-in catalog's maps/ is served verbatim, so if it does not parse, the
        # recorded code path hands a notebook something broken.
        import json
        maps_dir = BUILTIN_DATA_DIR / "maps"
        entries = json.loads((maps_dir / "index.json").read_text(encoding="utf-8"))
        assert entries, "no recorded maps are shipped"
        for entry in entries:
            text = (maps_dir / entry["file"]).read_text(encoding="utf-8")
            image, _x, _y = wire.parse_collision_map(text)
            expected = (360, 181) if entry["highResolution"] else (360, 185)
            assert image.shape == expected, entry["file"]

    def test_a_synthetic_snapshot_says_so_in_its_note(self):
        # A student who cannot tell invented data from measured data has learnt the wrong thing.
        import json
        maps_dir = BUILTIN_DATA_DIR / "maps"
        for entry in json.loads((maps_dir / "index.json").read_text(encoding="utf-8")):
            assert "NOT a real appliance response" in entry["note"], entry["file"]
