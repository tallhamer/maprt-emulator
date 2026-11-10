"""The built-in synthetic catalog: its rooms, patients and surfaces.

These guard the things that break a client test quietly — a room or patient the catalog lost, a
surface that stopped parsing.
"""



from maprt_emulator.settings import BUILTIN_DATA_DIR as DATA


class TestTheSurfaces:
    def test_every_surface_in_the_catalog_parses(self):
        import json

        from maprt_emulator.obj import parse_obj
        patients = json.loads((DATA / "patients.json").read_text(encoding="utf-8"))
        for surfaces in patients.values():
            for entry in surfaces:
                path = DATA / "surfaces" / f"{entry['id']}.obj"
                vertices, faces, _normals = parse_obj(path.read_bytes())
                assert len(vertices) > 1000 and len(faces) > 1000

    def test_they_are_roughly_person_shaped(self):
        # A plot of a surface should look like someone on a couch. If the generator ever produces
        # a cube, the notebook's 3D view is confusing rather than reassuring.
        import json

        from maprt_emulator.obj import bounding_box, parse_obj
        patients = json.loads((DATA / "patients.json").read_text(encoding="utf-8"))
        first = next(iter(patients.values()))[0]["id"]
        vertices, _faces, _normals = parse_obj(
            (DATA / "surfaces" / f"{first}.obj").read_bytes())
        low, high = bounding_box(vertices)
        length, width, height = high - low
        assert 1400 < length < 2200, f"{length:.0f} mm long is not a person"
        assert 250 < width < 900
        assert 100 < height < 700
        assert length > width > height
