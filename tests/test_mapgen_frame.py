"""The synthetic map's geometry: the surface frame (IEC 61217 room axes, z down), the couch sign,
and the isocenter registration per patient position."""

from types import SimpleNamespace

import numpy as np
import pytest

from maprt_emulator import mapgen

R = mapgen.HEAD_DISTANCE


def request(isocenter=(0.0, 0.0, 0.0), position="HFS", buffers=20.0):
    return SimpleNamespace(isocenter=SimpleNamespace(as_tuple=lambda: isocenter), patient_buffer=buffers,
                           couch_buffer=buffers, high_resolution=True, patient_position=position)


@pytest.mark.parametrize("couch, gantry, expected", [
    (0, 0, (0, 0, -R)),         # gantry 0: the head straight above (z points down)
    (0, 180, (0, 0, R)),
    (0, 90, (0, R, 0)),         # lateral, +y
    (90, 90, (-R, 0, 0)),       # couch +90 (counter-clockwise from above): out over the couch's foot end
    (270, 90, (R, 0, 0)),       # couch 270: toward the gantry, past a head-first patient's head
])
def test_source_offsets(couch, gantry, expected):
    np.testing.assert_allclose(mapgen.source_offsets(couch, gantry), expected, atol=1e-9)


def test_couch_does_not_matter_at_vertical():
    offsets = mapgen.source_offsets(np.array([270, 315, 0, 45, 90]), 0)
    np.testing.assert_allclose(offsets, np.broadcast_to(offsets[0], offsets.shape), atol=1e-9)


@pytest.mark.parametrize("position", sorted(mapgen.PATIENT_TO_MESH))
def test_patient_to_mesh_is_a_rotation(position):
    m = mapgen.PATIENT_TO_MESH[position]
    np.testing.assert_allclose(m @ m.T, np.eye(3))
    assert np.linalg.det(m) == pytest.approx(1.0)


@pytest.mark.parametrize("position, superior, left, posterior", [
    ("HFS", (1, 0, 0), (0, 1, 0), (0, 0, 1)),      # head toward the gantry, back down
    ("FFS", (-1, 0, 0), (0, -1, 0), (0, 0, 1)),
    ("HFP", (1, 0, 0), (0, -1, 0), (0, 0, -1)),
    ("HFDL", (1, 0, 0), (0, 0, 1), (0, -1, 0)),    # lying on the left side: left is down
])
def test_patient_axes_in_the_surface_frame(position, superior, left, posterior):
    m = mapgen.PATIENT_TO_MESH[position]
    np.testing.assert_array_equal(m @ (0, 0, 1), superior)
    np.testing.assert_array_equal(m @ (1, 0, 0), left)
    np.testing.assert_array_equal(m @ (0, 1, 0), posterior)


def box_cloud(x=(-1800.0, 80.0), y=(-180.0, 180.0), z=(-100.0, 150.0), step=20.0):
    """The surface points of a box: a crude body lying along x, its head end at +x."""
    xs, ys, zs = (np.linspace(a, b, round((b - a) / step) + 1) for a, b in (x, y, z))
    faces = []
    for fixed_axis, values in ((0, x), (1, y), (2, z)):
        for value in values:
            grids = np.meshgrid(*[(np.array([value]) if i == fixed_axis else g)
                                  for i, g in enumerate((xs, ys, zs))], indexing="ij")
            faces.append(np.stack([g.ravel() for g in grids], axis=1))
    return np.concatenate(faces)


def test_registered_isocenter_follows_the_patient_position():
    model = mapgen.SurfaceModel(box_cloud(), "box", registered=True)
    np.testing.assert_allclose(model.isocenter_in_mesh_frame((0, 0, 100), "HFS"), (100, 0, 0))
    np.testing.assert_allclose(model.isocenter_in_mesh_frame((0, 0, 100), "FFS"), (-100, 0, 0))
    np.testing.assert_allclose(model.isocenter_in_mesh_frame((10, 20, 0), "HFS"), (0, 10, 20))
    np.testing.assert_allclose(model.isocenter_in_mesh_frame((0, 0, 100), "UNKNOWN"), (100, 0, 0))


def test_catalog_surfaces_are_turned_into_the_frame():
    cloud = box_cloud()
    catalog = mapgen.SurfaceModel(cloud, "cat", registered=False)
    np.testing.assert_allclose(catalog.lower, (-80, -180, -150))      # x and z mirrored
    np.testing.assert_allclose(catalog.upper, (1800, 180, 100))
    centre = (catalog.lower + catalog.upper) / 2
    np.testing.assert_allclose(catalog.isocenter_in_mesh_frame((0, 0, 0), "HFS"), centre)
    assert catalog.couch_top == pytest.approx(100 + mapgen.COUCH_DROP)   # below the body: largest z


def test_a_head_end_isocenter_is_clear_past_the_head_and_blocked_over_the_body():
    """The appliance's signature for a cranial isocenter: beams whose source swings out past the
    head are clear, those swinging over the body are not. The old couch sign reversed this."""
    model = mapgen.SurfaceModel(box_cloud(), "box", registered=True)
    couch, gantry, safe = mapgen.compute_map(model, request())
    c, g = np.meshgrid(np.array(couch, float), np.array(gantry, float))
    toward_gantry = mapgen.source_offsets(c, g)[..., 0] / R
    blocked = safe == 0
    past_head, over_body = toward_gantry > 0.5, toward_gantry < -0.5
    assert blocked[past_head].mean() < 0.2
    assert blocked[over_body].mean() > 0.6
