"""The numpy OBJ reader: 1-based indices, the shapes MapRT emits, and the empty-mesh failures."""

import numpy as np
import pytest

from maprt_emulator.obj import ObjParseError, bounding_box, describe, parse_obj

# What a MapRT surface export actually looks like: a comment banner, the non-standard ps/fs
# counts, then v and f lines. Two triangles sharing an edge.
MAPRT_STYLE = """\
# VisionRT Ltd.
# File type: ASCII OBJ
ps 4
fs 2

v 0 0 0
v 10 0 0
v 10 20 0
v 0 20 0
f 1 2 3
f 1 3 4
"""


class TestParseObj:
    def test_reads_vertices_and_faces(self):
        vertices, faces, normals = parse_obj(MAPRT_STYLE)
        assert vertices.shape == (4, 3)
        assert faces.shape == (2, 3)
        assert normals is None

    def test_face_indices_are_converted_to_zero_based(self):
        # "f 1 2 3" addresses the first three vertices. An off-by-one here produces a mesh that
        # looks almost right, which is why it gets its own test.
        _vertices, faces, _ = parse_obj(MAPRT_STYLE)
        assert faces[0].tolist() == [0, 1, 2]
        assert faces[1].tolist() == [0, 2, 3]

    def test_accepts_bytes_as_the_api_returns_them(self):
        vertices, _, _ = parse_obj(MAPRT_STYLE.encode("utf-8"))
        assert len(vertices) == 4

    def test_reads_vertex_normals_when_present(self):
        text = MAPRT_STYLE + "vn 0 0 1\nvn 0 0 1\nvn 0 0 1\nvn 0 0 1\n"
        _vertices, _faces, normals = parse_obj(text)
        assert normals.shape == (4, 3)

    def test_slash_separated_face_fields_take_the_vertex_index(self):
        text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1/1/1 2/2/2 3/3/3\n"
        _vertices, faces, _ = parse_obj(text)
        assert faces[0].tolist() == [0, 1, 2]

    def test_a_quad_is_fan_triangulated(self):
        text = "v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf 1 2 3 4\n"
        _vertices, faces, _ = parse_obj(text)
        assert faces.shape == (2, 3)


class TestFailures:
    def test_no_vertices_is_an_error(self):
        with pytest.raises(ObjParseError, match="No vertices"):
            parse_obj("this is not an obj file at all\n")

    def test_vertices_without_faces_is_an_error(self):
        # A point cloud registers as "loaded" otherwise, and the emptiness surfaces much later.
        with pytest.raises(ObjParseError, match="no faces"):
            parse_obj("v 0 0 0\nv 1 0 0\n")

    def test_an_out_of_range_face_index_is_an_error(self):
        with pytest.raises(ObjParseError, match="1-based"):
            parse_obj("v 0 0 0\nv 1 0 0\nv 1 1 0\nf 1 2 9\n")


class TestSummaries:
    def test_bounding_box(self):
        vertices, _, _ = parse_obj(MAPRT_STYLE)
        low, high = bounding_box(vertices)
        assert np.allclose(low, [0, 0, 0])
        assert np.allclose(high, [10, 20, 0])

    def test_describe_mentions_counts_and_extent(self):
        vertices, faces, _ = parse_obj(MAPRT_STYLE)
        text = describe(vertices, faces)
        assert "4 vertices" in text
        assert "2 faces" in text
        assert "10 x 20 x 0 mm" in text
