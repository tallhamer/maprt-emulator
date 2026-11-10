"""Validate a data directory without starting a server — ``maprt-emulator --check-data``.

The loaders refuse to start on anything they cannot read, which is the right behaviour for a
server but a slow way to iterate when you are adding data: edit, start, read a traceback, stop,
repeat. This does the same loading, then goes further and reports the problems that are *not*
fatal but are almost always mistakes — a map recorded against a surface id that no longer exists,
an ``.obj`` nobody references, a plan naming a patient the appliance has never heard of.

Nothing here changes what the server will do. It tells you what it is about to do.
"""

from pathlib import Path

from maprt_emulator.library import load_library
from maprt_emulator.maps import MapLibrary, MapLibraryError
from maprt_emulator.store import Catalog, CatalogError


class Report:
    """Findings, split by consequence.

    Note what the split is *not*: whether the loader raises. A duplicate room GUID and a
    truncated recorded map both load perfectly happily and then serve wrong answers, which is
    worse than refusing to start — so they are errors here even though the server would come up.
    """

    def __init__(self):
        self.errors = []      # the server will refuse to start
        self.warnings = []    # it will start, but this is probably not what you meant
        self.notes = []       # worth seeing, not a problem

    def error(self, message):
        """Something that will produce wrong behaviour. Not necessarily a startup failure."""
        self.errors.append(message)

    def warn(self, message):
        self.warnings.append(message)

    def note(self, message):
        self.notes.append(message)

    @property
    def ok(self):
        return not self.errors

    def render(self, data_dir):
        lines = [f"Checking {data_dir}", ""]
        for label, items, marker in (("ERROR", self.errors, "✗"),
                                     ("WARNING", self.warnings, "!"),
                                     ("", self.notes, "·")):
            for item in items:
                prefix = f"{marker} {label}: " if label else f"  {marker} "
                lines.append(f"{prefix}{item}")
        if self.errors:
            lines += ["", (f"{_count(len(self.errors), 'error')} — fix these before serving this "
                           f"data. Some of them will not stop the emulator starting; it will "
                           f"start and give wrong answers, which is worse.")]
        elif self.warnings:
            lines += ["", (f"No errors, {_count(len(self.warnings), 'warning')}. "
                           f"The emulator will run.")]
        else:
            lines += ["", "No problems found."]
        return "\n".join(lines)


def _count(number, noun):
    return f"{number} {noun}{'' if number == 1 else 's'}"


def check(data_dir):
    """Validate ``data_dir`` and return a :class:`Report`."""
    data_dir = Path(data_dir)
    report = Report()

    try:
        catalog = Catalog.load(data_dir)
    except CatalogError as exc:
        report.error(str(exc))
        return report      # nothing else can be checked without a catalog

    try:
        maps = MapLibrary.load(data_dir)
    except MapLibraryError as exc:
        report.error(str(exc))
        maps = None

    report.note(", ".join([
        _count(len(catalog.known_patients), "patient"),
        _count(len(catalog.surfaces), "surface"),
        _count(len(catalog.rooms), "room"),
        _count(len(maps) if maps else 0, "recorded map"),
    ]))

    _check_rooms(catalog, report)
    _check_surfaces(catalog, data_dir, report)
    _check_patients(catalog, report)
    if maps is not None:
        _check_maps(maps, catalog, report)
    _check_plans(catalog, data_dir, report)

    return report


def _check_rooms(catalog, report):
    ids = [room.id for room in catalog.rooms]
    duplicates = {i for i in ids if ids.count(i) > 1}
    for room_id in sorted(duplicates):
        # GetMap keys on the GUID, so two rooms sharing one makes the choice between them
        # arbitrary — and silently so.
        report.error(f"two or more rooms share the id {room_id}; room ids must be unique")

    names = [room.machine_name for room in catalog.rooms]
    shared = sorted({n for n in names if names.count(n) > 1})
    if shared:
        # Not a problem — the shipped data does this on purpose, and so do real sites.
        report.note(f"machine name(s) shared by several rooms: {', '.join(shared)} "
                    f"(GET /integration/rooms/{{machineName}} will return DuplicateKey for these)")

    for room in catalog.rooms:
        if len(room.machine_name) > 16:
            report.warn(f"room {room.name!r} has a machineName of {len(room.machine_name)} "
                        f"characters; the spec caps it at 16")


def _check_surfaces(catalog, data_dir, report):
    from maprt_emulator.obj import ObjParseError, parse_obj

    referenced = {surface.path.resolve() for surface in catalog.surfaces}
    for directory in (data_dir / "surfaces", data_dir / "local" / "surfaces"):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.obj")):
            if path.resolve() not in referenced:
                report.warn(f"{path.relative_to(data_dir)} is not listed for any patient in "
                            f"patients.json, so it will never be served")

    for surface in catalog.surfaces:
        try:
            vertices, faces, _normals = parse_obj(surface.path.read_bytes())
        except ObjParseError as exc:
            report.error(f"{surface.path.name} does not parse as an OBJ mesh: {exc}")
            continue
        if len(vertices) < 100 or len(faces) < 100:
            report.warn(f"{surface.path.name} has only {len(vertices)} vertices and "
                        f"{len(faces)} faces — is it the mesh you meant?")


def _check_patients(catalog, report):
    for patient_id in sorted(catalog.known_patients):
        if len(patient_id) > 64:
            report.warn(f"patient id {patient_id!r} is {len(patient_id)} characters; "
                        f"DICOM caps it at 64")
        if not catalog.surfaces_for(patient_id):
            # Legitimate and deliberately represented in the shipped data, but say so in case it
            # is a typo in a surface id rather than an intentionally empty patient.
            report.note(f"patient {patient_id} has no surfaces (a valid case: the API returns "
                        f"an empty list at success:true)")

    timestamps = [(p, [s.timestamp for s in catalog.surfaces_for(p)])
                  for p in catalog.known_patients]
    for patient_id, stamps in timestamps:
        if len(stamps) != len(set(stamps)):
            report.warn(f"patient {patient_id} has two surfaces with the same timeStamp; "
                        f"'use the most recent' becomes arbitrary")


def _check_maps(maps, catalog, report):
    known_surfaces = {surface.id for surface in catalog.surfaces}
    known_rooms = {room.id for room in catalog.rooms}

    for recording in maps.recordings:
        name = recording.path.name
        # Not fatal — the library simply never matches it — but it means a recording you went to
        # the trouble of capturing will silently never be served.
        if recording.surface_id not in known_surfaces:
            report.warn(f"{name} is recorded against surface {recording.surface_id}, which is "
                        f"not in patients.json — it will never be served")
        if recording.room_id not in known_rooms:
            report.warn(f"{name} is recorded against room {recording.room_id}, which is not in "
                        f"rooms.json — it will never be served")

        text = recording.text()
        if not text.strip().endswith("DONE"):
            report.error(f"{name} has no DONE terminator; it was captured from a truncated "
                         f"response and will parse into a wrong-shaped array")
        try:
            header = text.split("\n", 1)[0]
            couch_samples, gantry_samples = (int(v) for v in header.split(","))
        except ValueError:
            report.error(f"{name} does not start with a 'couch,gantry' header line")
            continue
        triples = len(text.split()) - 2
        if triples != couch_samples * gantry_samples:
            report.error(f"{name} promises {couch_samples * gantry_samples:,} triples but "
                         f"carries {triples:,}")


def _check_plans(catalog, data_dir, report):
    plans_dir = data_dir / "plans"
    if not plans_dir.is_dir():
        return
    try:
        import pydicom
    except ImportError:
        report.note("pydicom is not installed, so RT Plans were not checked")
        return

    for path in sorted(plans_dir.glob("*.dcm")):
        try:
            plan = pydicom.dcmread(path, stop_before_pixels=True)
        except Exception as exc:  # noqa: BLE001 — any unreadable file is worth reporting
            report.error(f"{path.name} does not read as DICOM: {type(exc).__name__}: {exc}")
            continue

        if getattr(plan, "Modality", None) != "RTPLAN":
            report.warn(f"{path.name} is {getattr(plan, 'Modality', 'unknown')}, not RTPLAN")
            continue

        patient_id = str(getattr(plan, "PatientID", ""))
        if patient_id not in catalog.known_patients:
            # The capstone notebook looks this patient up; a mismatch fails its second cell.
            report.warn(f"{path.name} is for patient {patient_id!r}, who is not in "
                        f"patients.json — a notebook using this plan will fail")

        machines = {str(b.TreatmentMachineName) for b in getattr(plan, "BeamSequence", [])
                    if hasattr(b, "TreatmentMachineName")}
        configured = {room.machine_name for room in catalog.rooms}
        for machine in sorted(machines - configured):
            report.warn(f"{path.name} names machine {machine!r}, which is not in rooms.json")


def check_packs(settings, report):
    """Add the packs the settings select to ``report``: a refusal, or a skipped pack, is an error
    (a packs folder can be checked before a workshop); a mixed-version warning is a warning; the rest
    are notes."""
    try:
        library = load_library(settings)
    except CatalogError as exc:
        report.error(f"packs: {exc}")
        return
    if library is None:
        report.note("packs: off (--no-packs)" if settings.no_packs else "packs: none (no --packs-dir)")
        return
    report.note(f"packs: {library.summary()}")
    for entry in library.skipped:
        report.error(f"packs: {entry.name or entry.path} ({entry.path}) failed its checks and would be skipped: "
                     + "; ".join(map(str, entry.findings)))
    for warning in library.warnings:
        report.warn(f"packs: {warning}")
    for notice in library.notices:
        report.note(f"packs: {notice}")


def main(data_dir, settings=None):
    """Print the report; return a process exit code. With ``settings``, the packs they select are
    checked too."""
    report = check(data_dir)
    if settings is not None:
        check_packs(settings, report)
    print(report.render(data_dir))
    return 0 if report.ok else 1

