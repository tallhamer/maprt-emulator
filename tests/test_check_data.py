"""`maprt-emulator --check-data`: the validator someone leans on while adding data.

Its job is to catch the mistakes that are easy to make and hard to notice — a map recorded
against a surface id that no longer exists, an `.obj` nobody references, a recording that was
captured from a truncated response. Each of those loads without complaint and then produces
wrong behaviour, which is worse than a startup failure.
"""

import json
import shutil

import pytest

from maprt_emulator.check import check


@pytest.fixture
def catalog(data_dir):
    """A copy of the shipped catalog plus the plans directory, which `data_dir` omits."""
    from pathlib import Path
    source = Path(__file__).resolve().parent / "data" / "plans"  # audit: allow writable-locations - a test fixture, only read
    if source.is_dir():
        shutil.copytree(source, data_dir / "plans")
    return data_dir


def messages(report):
    return " | ".join(report.errors + report.warnings + report.notes)


class TestTheShippedDataIsClean:
    def test_no_errors_and_no_warnings(self, catalog):
        report = check(catalog)
        assert report.errors == [], report.errors
        assert report.warnings == [], report.warnings

    def test_it_counts_what_it_found(self, catalog):
        assert "3 patients" in messages(check(catalog))

    def test_the_deliberate_shared_machine_name_is_a_note_not_a_problem(self, catalog):
        # Two rooms share "TrueBeam" on purpose, so DuplicateKey can be taught.
        report = check(catalog)
        assert any("TrueBeam" in note for note in report.notes)
        assert report.ok

    def test_the_patient_with_no_surfaces_is_a_note_not_a_problem(self, catalog):
        report = check(catalog)
        assert any("ANON003" in note for note in report.notes)
        assert report.ok


class TestItCatchesErrors:
    def test_a_missing_data_directory(self, tmp_path):
        report = check(tmp_path / "nowhere")
        assert not report.ok
        assert "--data-dir" in messages(report)

    def test_two_rooms_sharing_a_guid(self, catalog):
        # GetMap keys on the GUID, so this makes the choice between them arbitrary — and the
        # server starts perfectly happily, which is what makes it worth catching here.
        rooms = json.loads((catalog / "rooms.json").read_text(encoding="utf-8"))
        rooms[1]["id"] = rooms[0]["id"]
        (catalog / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
        report = check(catalog)
        assert not report.ok
        assert "unique" in messages(report)

    def test_a_surface_that_is_not_a_mesh(self, catalog):
        surface = next((catalog / "surfaces").glob("*.obj"))
        surface.write_text("this is not an OBJ file\n", encoding="utf-8")
        assert not check(catalog).ok

    def test_a_recorded_map_with_no_DONE(self, catalog):
        body = next((catalog / "maps").glob("*.txt"))
        body.write_text(body.read_text(encoding="utf-8").replace("DONE", ""), encoding="utf-8")
        report = check(catalog)
        assert not report.ok
        assert "DONE" in messages(report)

    def test_a_recorded_map_whose_count_disagrees_with_its_header(self, catalog):
        body = next((catalog / "maps").glob("*.txt"))
        lines = body.read_text(encoding="utf-8").splitlines()
        body.write_text("\n".join(lines[:1] + lines[5:]), encoding="utf-8")  # drop four triples
        report = check(catalog)
        assert not report.ok
        assert "triples" in messages(report)


class TestItCatchesWarnings:
    def test_an_orphan_surface_file(self, catalog):
        """An .obj nobody lists can never be served, and nothing else would tell you."""
        existing = next((catalog / "surfaces").glob("*.obj"))
        shutil.copy(existing, catalog / "surfaces" / "orphan.obj")
        report = check(catalog)
        assert report.ok                       # it still runs
        assert "orphan.obj" in messages(report)

    def test_a_map_recorded_against_an_unknown_surface(self, catalog):
        entries = json.loads((catalog / "maps" / "index.json").read_text(encoding="utf-8"))
        entries[0]["surfaceId"] = "11111111-2222-3333-4444-555555555555"
        (catalog / "maps" / "index.json").write_text(json.dumps(entries), encoding="utf-8")
        report = check(catalog)
        assert report.ok
        assert "never be served" in messages(report)

    def test_a_map_recorded_against_an_unknown_room(self, catalog):
        entries = json.loads((catalog / "maps" / "index.json").read_text(encoding="utf-8"))
        entries[0]["roomId"] = "not-a-room"
        (catalog / "maps" / "index.json").write_text(json.dumps(entries), encoding="utf-8")
        assert "never be served" in messages(check(catalog))

    def test_an_over_long_patient_id(self, catalog):
        patients = json.loads((catalog / "patients.json").read_text(encoding="utf-8"))
        patients["X" * 70] = []
        (catalog / "patients.json").write_text(json.dumps(patients), encoding="utf-8")
        assert "64" in messages(check(catalog))

    def test_an_over_long_machine_name(self, catalog):
        rooms = json.loads((catalog / "rooms.json").read_text(encoding="utf-8"))
        rooms[0]["machineName"] = "A" * 20
        (catalog / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
        assert "16" in messages(check(catalog))

    def test_two_surfaces_with_the_same_timestamp(self, catalog):
        # "Use the most recent" becomes arbitrary.
        patients = json.loads((catalog / "patients.json").read_text(encoding="utf-8"))
        patients["ANON001"][1]["timeStamp"] = patients["ANON001"][0]["timeStamp"]
        (catalog / "patients.json").write_text(json.dumps(patients), encoding="utf-8")
        assert "timeStamp" in messages(check(catalog))


class TestItChecksPlans:
    def test_the_shipped_plan_lines_up_with_the_catalog(self, catalog):
        assert check(catalog).ok

    def test_a_plan_for_an_unknown_patient_warns(self, catalog):
        pytest.importorskip("pydicom")
        import pydicom
        plan_path = next((catalog / "plans").glob("*.dcm"))
        plan = pydicom.dcmread(plan_path)
        plan.PatientID = "SOMEONE_ELSE"
        plan.save_as(plan_path, enforce_file_format=True)
        report = check(catalog)
        assert "SOMEONE_ELSE" in messages(report)
        assert report.ok                       # a warning, not an error

    def test_a_plan_naming_an_unconfigured_machine_warns(self, catalog):
        pytest.importorskip("pydicom")
        import pydicom
        plan_path = next((catalog / "plans").glob("*.dcm"))
        plan = pydicom.dcmread(plan_path)
        for beam in plan.BeamSequence:
            beam.TreatmentMachineName = "NoSuchLinac"
        plan.save_as(plan_path, enforce_file_format=True)
        assert "NoSuchLinac" in messages(check(catalog))


class TestTheReport:
    def test_it_does_not_claim_errors_always_stop_the_server(self, catalog):
        """Several errors load fine and then serve wrong answers.

        An earlier version of the summary said "the emulator will not start", which was false
        for the duplicate-GUID and truncated-map cases — and reassuring in exactly the wrong
        direction, since those are worse than a refusal to start.
        """
        rooms = json.loads((catalog / "rooms.json").read_text(encoding="utf-8"))
        rooms[1]["id"] = rooms[0]["id"]
        (catalog / "rooms.json").write_text(json.dumps(rooms), encoding="utf-8")
        rendered = check(catalog).render(catalog)
        assert "will not start" not in rendered
        assert "wrong answers" in rendered

    def test_a_clean_catalog_says_so(self, catalog):
        assert "No problems found." in check(catalog).render(catalog)

    def test_counts_are_singular_where_they_should_be(self, catalog):
        # "1 recorded maps" in the first line someone reads is a small thing that makes the
        # rest look careless.
        summary = check(catalog).notes[0]
        assert "1 recorded map" in summary and "1 recorded maps" not in summary
        assert "3 patients" in summary
