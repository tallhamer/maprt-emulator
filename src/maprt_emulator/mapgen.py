"""Computing a plausible clearance map, and writing it in the GetMap wire format.

**This is not a collision engine and its answers are not physics.** It is a small geometric model
chosen so the map *responds to the request the way a real one does*, which is what makes it
teachable: widen a buffer and the collision region grows, kick the couch and the safe gantry band
narrows into the bow-tie shape a planner recognises, move the isocenter towards the shoulder and
the map gets worse on one side. A student can reason about what they see and be right about the
reasoning, even though the numbers are invented.

What it is built on:

* The **real surface mesh** of the requested patient, decimated to a point cloud. So two patients
  genuinely get different maps, for the right reason — their bodies are different shapes — rather
  than from a hash of their id.
* The gantry head modelled as a sphere of :data:`HEAD_RADIUS` at :data:`HEAD_DISTANCE` from the
  isocenter, which is roughly where a linac's collimator housing sits.
* The couch modelled as a thin **slab** below the patient — a board, not a half-space. This
  matters more than it sounds: modelled as a half-space, the couch blocks every gantry angle
  below horizontal, so the whole of gantry 150-210 comes back as a collision at every couch
  angle. On a real linac the head swings freely *under* the board and a posterior beam is
  routinely clear, so a half-space produces maps that are obviously wrong to anyone who has
  seen a real one.

Not modelled: the couch pedestal and support arm. Those are a real source of collisions at large
couch kicks, so the emulator is more optimistic than reality at the inferior end.

**The frame is the one MapRT surfaces come in** (IEC 61217 room axes at couch 0): x toward the
gantry, y the room's lateral axis, z **down** toward the floor. A recorded surface (from a pack) is
already in it, registered so that the DICOM isocenter maps in through the patient position (see
:data:`PATIENT_TO_MESH`). The emulator's own catalog surfaces were built in a frame of their own
(head at low x, z up) and are turned into this one when loaded; the bytes served to clients do
not change.

The geometry worth understanding is where the source ends up. The gantry rotates in a fixed room
plane and the couch rotates the patient, so with gantry ``g`` and couch ``c`` (IEC 61217: couch
positive is counter-clockwise seen from above) the source sits at, relative to the isocenter and
in the surface's frame::

    toward the gantry (x)   -R * sin(g) * sin(c)
    lateral (y)              R * sin(g) * cos(c)
    down (z)                -R * cos(g)

Read that and the shape of every clearance map follows. At gantry 0 or 180 the ``sin(g)`` terms
vanish: the source is straight above or below the isocenter and **the couch angle cannot matter**.
At gantry 90 with the couch kicked to 90, the source swings a full ``R`` along the couch, out over
its foot end (for a head-first patient, the feet) — which is exactly where real collisions happen,
and why the safe region pinches as the couch rotates. With the couch at 270 the same gantry angle
swings out past the head instead, which is why cranial non-coplanar beams are so often clear.

Until 2026-11 the model wrote the first and third terms with the opposite signs and ignored the
patient position when placing the isocenter, so its head/feet asymmetry came out wrong or not at
all; scored against the recorded appliance maps in the published packs, the corrected frame gets
the direction of that asymmetry right in every pack whose recorded map has one.
"""

import hashlib
import logging

import numpy as np

logger = logging.getLogger("maprt_emulator.mapgen")

# The gantry head is modelled as a sphere, so these two numbers only matter through their
# difference: HEAD_DISTANCE - HEAD_RADIUS is how close the head's inner face comes to the
# isocenter, and that is what decides whether anything collides. A linac's collimator face sits
# roughly 360-400 mm out, so 550 - 180 = 370 mm is the physically defensible setting. It puts a
# default request (20 mm buffers, isocenter mid-torso) at around 78% clear on these surfaces,
# which is the right ballpark for a real clearance map: mostly deliverable, with collision bands
# at oblique angles that close up as the couch is kicked.
HEAD_DISTANCE = 550.0
HEAD_RADIUS = 180.0

#: Points the surface is decimated to before distance queries. The mesh has ~10k vertices; a
#: couple of thousand is plenty for a model this approximate and keeps a high-resolution map
#: under a second.
SURFACE_SAMPLE_POINTS = 1800

#: The couch board: how far its top sits below the patient's lowest point, its lateral
#: half-width, and its thickness. A board, not a half-space — see the module docstring.
COUCH_DROP = 25.0
COUCH_HALF_WIDTH = 250.0
COUCH_THICKNESS = 70.0

# -- the sampling grids -----------------------------------------------------------------------
#
# Gantry runs 0..359 and couch 270..359 and 0..90 (IEC 61217, i.e. couch +/-90 about zero).
#
# A coarse grid's step sizes have to divide the axes evenly, because the client-side parser
# upsamples by integer repetition: 360/gantry_samples and 180/(couch_samples-1) must both be
# whole numbers. 37 couch x 36 gantry is the specification's own example (5 deg couch, 10 deg
# gantry) and satisfies it.

# -- the frame ------------------------------------------------------------------------------
#
# DICOM patient axes (L, P, S) -> surface-frame axes, per patient position: a surface-frame vector
# is PATIENT_TO_MESH[position] @ (dicom vector). The transposes of the MapRT -> DICOM matrices the
# SGRT tools validate against real surfaces (rt_geometry.MapRTOrientTransform); signed
# permutations, so each inverse is exact. A contract test in the monorepo keeps the two in step.

_MAPRT_TO_DICOM = {
    "HFS": ((0, 1, 0), (0, 0, 1), (1, 0, 0)),
    "HFP": ((0, -1, 0), (0, 0, -1), (1, 0, 0)),
    "FFS": ((0, -1, 0), (0, 0, 1), (-1, 0, 0)),
    "FFP": ((0, 1, 0), (0, 0, -1), (-1, 0, 0)),
    "HFDL": ((0, 0, 1), (0, -1, 0), (1, 0, 0)),
    "HFDR": ((0, 0, -1), (0, 1, 0), (1, 0, 0)),
    "FFDL": ((0, 0, 1), (0, 1, 0), (-1, 0, 0)),
    "FFDR": ((0, 0, -1), (0, -1, 0), (-1, 0, 0)),
}
PATIENT_TO_MESH = {position: np.array(rows, dtype=np.float64).T for position, rows in _MAPRT_TO_DICOM.items()}

#: The catalog surfaces' own frame (head at low x, z up) -> the surface frame: a half turn about y.
CATALOG_TO_MESH = np.diag([-1.0, 1.0, -1.0])

HIGH_RES_COUCH_STEP, HIGH_RES_GANTRY_STEP = 1, 1
LOW_RES_COUCH_STEP, LOW_RES_GANTRY_STEP = 5, 10


def couch_angles(step):
    """Couch angles at ``step`` degrees, in emission order: +90 down through 0 to -90 (= 270).

    Descending, because that is the order the specification's example uses (``90,180`` then
    ``85,180`` then ``80,180``). The order carries no meaning — §5.2.6 says the list is not
    ordered — but matching it means a client that wrongly depends on order still sees realistic
    data from the emulator, and ``--shuffle-map`` is there to break that dependence on purpose.
    """
    descending = list(range(90, -1, -step))                 # 90 .. 0
    wrapped = list(range(360 - step, 269, -step))           # 355 .. 270
    return descending + wrapped


def gantry_angles(step):
    """Gantry angles at ``step`` degrees, starting at 180 — again matching the spec's example."""
    return list(range(180, 360, step)) + list(range(0, 180, step))


def grid_for(high_resolution):
    """``(couch_angles, gantry_angles)`` for the requested resolution."""
    if high_resolution:
        return couch_angles(HIGH_RES_COUCH_STEP), gantry_angles(HIGH_RES_GANTRY_STEP)
    return couch_angles(LOW_RES_COUCH_STEP), gantry_angles(LOW_RES_GANTRY_STEP)


# -- the model --------------------------------------------------------------------------------

class SurfaceModel:
    """A patient surface reduced to what the clearance model needs.

    Built once per surface and cached, because decimating a mesh is the slow part and the answer
    does not change between requests.
    """

    def __init__(self, vertices, surface_id="", registered=True):
        """`registered` is True for a recorded MapRT surface (a pack's), already in the surface
        frame with the isocenter registration of :meth:`isocenter_in_mesh_frame`; False for one of
        the emulator's catalog surfaces, which is turned into the frame here."""
        self.surface_id = surface_id
        self.registered = registered
        vertices = np.asarray(vertices, dtype=np.float64)
        if not registered:
            vertices = vertices @ CATALOG_TO_MESH.T

        # Even stride rather than a random sample: deterministic, and on a mesh built by sweeping
        # a profile it keeps coverage along the whole body instead of clumping.
        stride = max(1, len(vertices) // SURFACE_SAMPLE_POINTS)
        self.points = np.ascontiguousarray(vertices[::stride])

        self.centroid = vertices.mean(axis=0)
        self.lower = vertices.min(axis=0)
        self.upper = vertices.max(axis=0)

        # The couch surface: a little below the patient's lowest point. The frame's z points down,
        # so "below" is the largest z.
        self.couch_top = float(self.upper[2]) + COUCH_DROP

    @classmethod
    def from_obj_bytes(cls, data, surface_id="", registered=True):
        from maprt_emulator.obj import parse_obj
        vertices, _faces, _normals = parse_obj(data)
        return cls(vertices, surface_id, registered=registered)

    def isocenter_in_mesh_frame(self, isocenter, patient_position="HFS"):
        """Where a DICOM-coordinate isocenter sits in the surface frame.

        Either way the DICOM axes map onto the frame through the patient position
        (:data:`PATIENT_TO_MESH`); an unknown position is read as HFS.

        * **A recorded surface** is registered: the mapped isocenter is used as it is. Scored
          against the recorded appliance maps in the published packs, this is the registration
          that reproduces where their collisions fall.
        * **A catalog surface** carries no registration (§3.2.1: the mesh has no patient position
          metadata), so **this is the emulator's invention**: the isocenter is an offset from the
          centre of the patient's bounding box. ``(0, 0, 0)`` lands mid-torso, and moving the
          isocenter moves the map the way you would expect. Do not read anything into the absolute
          numbers; the *relative* behaviour is the lesson.
        """
        matrix = PATIENT_TO_MESH.get(patient_position, PATIENT_TO_MESH["HFS"])
        offset = matrix @ np.asarray(isocenter, dtype=np.float64)
        if self.registered:
            return offset
        return (self.lower + self.upper) / 2.0 + offset

    def clearance(self, source_points):
        """Smallest distance from each source point to the patient surface, in mm.

        ``source_points`` is ``(n, 3)`` in the mesh frame. Chunked so a 181x360 map does not try
        to materialise a 65,160 x 1,800 distance matrix at once.
        """
        source_points = np.asarray(source_points, dtype=np.float64)
        out = np.empty(len(source_points), dtype=np.float64)
        chunk = max(1, 4_000_000 // max(1, len(self.points)))
        for start in range(0, len(source_points), chunk):
            block = source_points[start:start + chunk]
            deltas = block[:, None, :] - self.points[None, :, :]
            out[start:start + chunk] = np.sqrt((deltas ** 2).sum(axis=2)).min(axis=1)
        return out


def _distance_to_box(points, centre, half_extents):
    """Shortest distance from each point to an axis-aligned box. Zero inside it.

    The standard form: push the point into the box's own octant, subtract the half extents, clamp
    the negatives to zero (those axes are already inside) and take the norm of what is left.
    """
    delta = np.abs(np.asarray(points, dtype=np.float64) - np.asarray(centre)) - np.asarray(half_extents)
    return np.linalg.norm(np.maximum(delta, 0.0), axis=-1)


def source_offsets(couch, gantry, distance=None):
    """Gantry-head centres relative to the isocenter, in the surface frame.

    ``couch`` and ``gantry`` are broadcastable arrays of degrees (IEC 61217). Returns ``(..., 3)``
    as ``(toward_gantry, lateral, down)`` — the three expressions from this module's docstring.

    ``distance`` defaults to :data:`HEAD_DISTANCE`, resolved at call time rather than as a
    default argument value. A default argument would capture the constant at import, so changing
    the constant would silently have no effect — which is exactly the trap it was: a parameter
    sweep over HEAD_DISTANCE reported identical results for every value.
    """
    distance = HEAD_DISTANCE if distance is None else distance
    g = np.deg2rad(np.asarray(gantry, dtype=np.float64))
    c = np.deg2rad(np.asarray(couch, dtype=np.float64))
    g, c = np.broadcast_arrays(g, c)
    sin_g = np.sin(g)
    return np.stack([
        -distance * sin_g * np.sin(c),     # toward the gantry (x)
        distance * sin_g * np.cos(c),      # lateral (y)
        -distance * np.cos(g),             # down (z): gantry 0 puts the head above
    ], axis=-1)


def _habitus_margin(surface_id, seed):
    """A small per-surface, per-seed margin in mm, so runs are varied but reproducible.

    Everything else in the model is geometric. This exists only so that ``--seed`` does something
    and two surfaces of similar shape are not pixel-identical; it is deliberately too small to
    change whether a map looks plausible.
    """
    digest = hashlib.sha256(f"{surface_id}:{seed}".encode()).digest()
    return (digest[0] / 255.0 - 0.5) * 20.0      # +/- 10 mm


def is_safe_at(surface, request, couch, gantry, seed=0):
    """Whether the gantry is clear at each ``(couch, gantry)`` pair. Returns 0/1 int8.

    **The single evaluation function.** :func:`compute_map` calls it over a grid and
    GetBeamDeliveryStatus calls it at a plan's control points, so the two endpoints cannot
    disagree about the same angle: a client that checks beams against a downloaded map gets the
    same verdict the server gives. That agreement is the point of routing both through here
    rather than writing the test twice.

    ``couch`` and ``gantry`` are arrays of degrees, broadcast together. Angles need not be whole
    degrees — which is how GetBeamDeliveryStatus can be more precise than any map: a map is
    *sampled*, and a control point at gantry 185.4 falls between two of its rows.
    """
    couch = np.asarray(couch, dtype=np.float64).ravel()
    gantry = np.asarray(gantry, dtype=np.float64).ravel()

    isocenter = surface.isocenter_in_mesh_frame(request.isocenter.as_tuple(),
                                                getattr(request, "patient_position", "HFS"))
    sources = isocenter + source_offsets(couch, gantry)

    margin = _habitus_margin(surface.surface_id, seed)

    # Patient clearance: the head is a sphere, so it collides when the surface comes within
    # HEAD_RADIUS + the requested patient buffer of the head's centre.
    patient_gap = (surface.clearance(sources)
                   - HEAD_RADIUS - request.patient_buffer - margin)

    # Couch clearance: distance from the head's centre to the couch board, treated as a box.
    # Because it is a board rather than a half-space, a head swung below it is *past* it and
    # clear — which is what lets posterior beams be deliverable, as they are in reality.
    couch_centre = np.array([
        (surface.lower[0] + surface.upper[0]) / 2.0,          # centred on the body's length
        (surface.lower[1] + surface.upper[1]) / 2.0,          # and on its midline
        surface.couch_top + COUCH_THICKNESS / 2.0,             # z points down
    ])
    couch_half = np.array([
        (surface.upper[0] - surface.lower[0]) / 2.0 + 150.0,  # a little longer than the patient
        COUCH_HALF_WIDTH,
        COUCH_THICKNESS / 2.0,
    ])
    couch_gap = (_distance_to_box(sources, couch_centre, couch_half)
                 - HEAD_RADIUS - request.couch_buffer)

    return ((patient_gap > 0) & (couch_gap > 0)).astype(np.int8)


def compute_map(surface, request, seed=0):
    """The clearance grid for a GetMap request.

    Returns ``(couch_angles, gantry_angles, safe)`` where ``safe`` is a ``(gantry, couch)`` array
    of 0/1 — 1 meaning clear. The array is indexed by position in the returned angle lists, which
    is **emission order**, not the display order the client-side parser rearranges it into.
    """
    couch, gantry = grid_for(request.high_resolution)

    # (gantry, couch) grids, so the result is indexed [gantry_index, couch_index].
    gantry_grid, couch_grid = np.meshgrid(np.array(gantry, dtype=np.float64),
                                          np.array(couch, dtype=np.float64), indexing="ij")

    safe = is_safe_at(surface, request, couch_grid, gantry_grid, seed=seed)
    return couch, gantry, safe.reshape(len(gantry), len(couch))


def beam_verdicts(surface, request, seed=0):
    """``[(beam_number, is_safe), …]`` for the beams in a GetBeamDeliveryStatus request.

    A beam is deliverable when **every control point it carries** is clear. The service checks
    the control points one by one (§2.3.2), which is why it is so much quicker than computing a
    whole clearance map: it looks at a handful of angles instead of tens of thousands.

    Which means: **the server only checks what you send it.** Two control points describe two
    angles, not the arc between them. A plan's arc sent as its start and stop will be reported
    safe while sweeping straight through a collision. That is not an emulator quirk — it is what
    the endpoint is for, and it is why a client-side sweep against a full map answers a different
    question.
    """
    verdicts = []
    for beam in request.beam_list:
        couch = [point.couch_angle for point in beam.control_points]
        gantry = [point.gantry_angle for point in beam.control_points]
        if not couch:
            # No control points is no evidence of safety. Reporting True for an empty beam would
            # be the most dangerous possible default.
            verdicts.append((beam.beam_number, False))
            continue
        safe = is_safe_at(surface, request, couch, gantry, seed=seed)
        verdicts.append((beam.beam_number, bool(safe.all())))
    return verdicts


# -- the wire format --------------------------------------------------------------------------

def map_lines(couch, gantry, safe, shuffle=False, rng=None):
    """Yield the GetMap response body, line by line (spec §5.2.6).

    The header is ``couch_samples,gantry_samples``; then one ``couch,gantry,isOK`` triple per
    combination; then ``DONE``. ``isOK`` is 1 for clear and 0 for a collision. Every line ends in
    CRLF, as the appliance's do (recorded).

    ``shuffle`` randomises the triple order. §5.2.6 says the list is not ordered, so a client must
    not depend on the order — this makes that testable instead of theoretical.
    """
    yield f"{len(couch)},{len(gantry)}\r\n"

    triples = [f"{c},{g},{int(safe[gi, ci])}\r\n"
               for gi, g in enumerate(gantry)
               for ci, c in enumerate(couch)]
    if shuffle:
        (rng or np.random.default_rng()).shuffle(triples)
    yield from triples

    yield "DONE\r\n"


def map_text(couch, gantry, safe, **kwargs):
    """The whole response body as one string. For recording maps and for tests."""
    return "".join(map_lines(couch, gantry, safe, **kwargs))
