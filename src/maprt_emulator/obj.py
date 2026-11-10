"""A minimal ASCII OBJ reader — enough for a MapRT patient surface, in numpy.

The emulator's own, so it depends on no client library: it reads a surface to build its clearance
model (:mod:`maprt_emulator.mapgen`) and to validate a data folder (:mod:`maprt_emulator.check`).
VTK would do the job too, as a 200 MB dependency for two kinds of line. The format:

    # VisionRT Ltd.
    # File type: ASCII OBJ
    ps 265857                 <- vertex count (not standard OBJ; ignored here)
    fs 509284                 <- face count (ditto)

    v -258.48 -302.746 27.4538
    ...
    f 2872 3395 3396
    ...

Surfaces served by the appliance's ``GET /integration/surfaces/{id}`` carry only ``v`` and ``f``
lines, CRLF-terminated, with no header (seen in recordings, 2026-10-23); the header above is what
the workshop's sample files carry. Both read the same.

Face indices are **1-based** — the single most common mistake when hand-rolling an OBJ reader,
because an off-by-one silently produces a mesh that looks almost right. ``f a/b/c`` forms
(vertex/texture/normal) are tolerated by taking the first field, and ``vn`` normals are read when
present, so this also handles OBJs from other sources.

Remember what the bytes mean: a MapRT surface is in **NATIVE (couch) coordinates** and carries no
patient-position metadata, so these vertices are not in the DICOM patient frame until you
transform them.
"""

import numpy as np


class ObjParseError(Exception):
    """The bytes handed in are not a usable OBJ mesh."""


def parse_obj(data):
    """Parse OBJ bytes (or text) into ``(vertices, faces, normals)``.

    ``vertices`` is ``(n, 3)`` float64, ``faces`` is ``(m, 3)`` int32 of **0-based** indices into
    ``vertices``, and ``normals`` is ``(n, 3)`` float64 or ``None`` when the file has none.

    A face with more than three vertices is fan-triangulated. Raises :class:`ObjParseError` when
    there are no vertices or no faces — an empty mesh is the failure mode that otherwise shows up
    much later as a reconstruction with nothing in it, far from the file that caused it.
    """
    if isinstance(data, bytes | bytearray):
        text = data.decode("utf-8", errors="replace")
    else:
        text = data

    vertices = []
    normals = []
    faces = []

    for line in text.splitlines():
        # Cheapest possible discrimination: every line we care about starts with v, vn or f.
        if not line or line[0] not in "vf":
            continue
        parts = line.split()
        tag = parts[0]
        if tag == "v":
            vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "vn":
            normals.append((float(parts[1]), float(parts[2]), float(parts[3])))
        elif tag == "f":
            # "f 1 2 3", "f 1/1/1 2/2/2 3/3/3" and "f 1//1 …" all give the vertex index first.
            # OBJ indices are 1-based, so subtract one.
            idx = [int(field.split("/")[0]) - 1 for field in parts[1:]]
            # Fan-triangulate anything with more than three corners. MapRT only emits triangles,
            # but a quad mesh from another tool should not silently lose geometry.
            for k in range(1, len(idx) - 1):
                faces.append((idx[0], idx[k], idx[k + 1]))

    if not vertices:
        raise ObjParseError("No vertices found; this is probably not an OBJ file.")
    if not faces:
        raise ObjParseError("Vertices but no faces, so this is a point cloud and not a mesh.")

    vertices = np.asarray(vertices, dtype=np.float64)
    faces = np.asarray(faces, dtype=np.int32)

    if faces.min() < 0 or faces.max() >= len(vertices):
        raise ObjParseError(
            f"Face indices run {faces.min() + 1}..{faces.max() + 1} (1-based) but the file has "
            f"{len(vertices)} vertices."
        )

    return vertices, faces, (np.asarray(normals, dtype=np.float64) if normals else None)


def bounding_box(vertices):
    """``(min_xyz, max_xyz)`` of a vertex array, as two length-3 arrays."""
    return vertices.min(axis=0), vertices.max(axis=0)


def describe(vertices, faces):
    """A one-line human summary of a mesh, for notebook output."""
    low, high = bounding_box(vertices)
    extent = high - low
    return (f"{len(vertices):,} vertices, {len(faces):,} faces, "
            f"extent {extent[0]:.0f} x {extent[1]:.0f} x {extent[2]:.0f} mm")
