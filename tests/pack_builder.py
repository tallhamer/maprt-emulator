"""Writes small session packs for the tests, with the standard library (and pydicom for a DICOM file).

The emulator can't use ``maprt-pack`` (a workspace package), so this writes the format by hand:
one patient, one session, a call per endpoint. :func:`clean_files` is a pack that passes every
check; a test edits the ``{path: bytes}`` it returns to seed a failure, then :func:`write_pack`
adds the manifest (sha256s of whatever the files are by then) and writes a folder or a ``.zip``.
"""

import base64
import hashlib
import io
import json
import re
import zipfile

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

PATIENT = "ANON-1"
SESSION = "11111111-2222-4333-8444-555555555555"
ROOM = "96bbc235-0da9-e983-0783-5973a7d11144"
SURFACE = "5f0c1a2b-3c4d-4e5f-8a6b-7c8d9e0f1a2b"
SERVER = "Saturn/1.3.217.2"
OBJ = "v 0 0 0\r\nv 1 0 0\r\nv 0 1 0\r\nf 1 2 3\r\n"
MAP = b"37,36\r\n0,0,1\r\nDONE\r\n"
CT_FILE = "dicom/ct/CT.1.dcm"  # audit: allow path-construction - a zip entry name, "/" on every OS


def ok(data):
    # The appliance's keys, in its order (recorded).
    return {"data": data, "errorData": None, "success": True, "errorCode": None, "errorCodeType": "NoErrorCodes"}


def record_pair(stem, method, url, body, reply, *, status=200, session=SESSION):
    """``{local file name: record}`` for one call: its request and (unless ``reply`` is ``None``) its response."""
    stamp = re.search(r"(\d{8})-(\d{2})(\d{2})(\d{2})\.(\d{6})", stem)
    sent = f"{stamp[1][:4]}-{stamp[1][4:6]}-{stamp[1][6:]}T{stamp[2]}:{stamp[3]}:{stamp[4]}.{stamp[5]}"
    request = {"session": session, "method": method, "url": url, "sent": sent,
               "headers": {"accept": "*/*", "authorization": "Bearer ***redacted***"}, "body": body}
    files = {f"{stem}_request.json": request}
    if reply is not None:
        files[f"{stem}_response.json"] = {"session": session, "request_file": f"{stem}_request.json",
                                          "received": sent, "elapsed_ms": 5.0, "status": status,
                                          "headers": {"server": SERVER}, **reply}
    return files


def calls():
    """One recorded call per endpoint, sent a second apart (``sent`` is read from each stem's timestamp)."""
    surface = "data:application/octet-stream;base64," + base64.b64encode(OBJ.encode()).decode()
    pairs = [
        ("integration_ping_20261020-090001.000000", "GET", "/integration/ping", None, {"body": ok("2.0.0")}),
        ("integration_rooms_20261020-090002.000000", "GET", "/integration/rooms", None,
         {"body": ok([{"name": "TB1", "id": ROOM, "machineName": "Truebeam", "coordinateSystem": "IEC_61217"}])}),
        (f"integration_patients_{PATIENT}_surfaces_20261020-090003.000000", "GET",
         f"/integration/patients/{PATIENT}/surfaces", None,
         {"body": ok([{"id": SURFACE, "timeStamp": "2026-10-20T08:00:00.1", "timeZone": "UTC"}])}),
        (f"integration_surfaces_{SURFACE}_20261020-090004.000000", "GET", f"/integration/surfaces/{SURFACE}", None,
         {"body": ok(surface)}),
        ("integration_GetMap_20261020-090005.000000", "POST", "/integration/GetMap",
         {"CouchBuffer": 20, "PatientBuffer": 20.0, "HighResolution": False, "PatientPosition": "HFS",
          "PatientSurfaceId": SURFACE, "TreatmentRoomId": ROOM, "Isocenter": {"x": 0.0, "y": 98.6, "z": -1.71}},
         {"body_file": "integration_GetMap_20261020-090005.000000_response.txt"}),
        ("integration_GetBeamDeliveryStatus_20261020-090006.000000", "POST", "/integration/GetBeamDeliveryStatus",
         {"CouchBuffer": 20.0, "PatientBuffer": 20.0, "PatientPosition": "HFS", "PatientSurfaceId": SURFACE,
          "TreatmentRoomId": ROOM, "Isocenter": {"x": 0.0, "y": 98.6, "z": -1.71},
          "BeamList": [{"beamNumber": "1", "controlPoints": [{"gantryAngle": 0, "couchAngle": 0}]}]},
         {"body": ok({"statuses": [{"beamNumber": 1, "isSafe": True}]})}),
    ]
    files = {}
    for stem, method, url, body, reply in pairs:
        files.update(record_pair(stem, method, url, body, reply))
    return files


def clean_files(*, dicom=True):
    """``{path in the pack: bytes}`` for a pack that passes every check, without its manifest."""
    prefix = f"sessions/{SESSION}/"
    files = {prefix + "session.json": as_json({"format": "maprt-recording/1", "session": SESSION,
                                               "started": "2026-10-20T09:00:00.000000", "api_url": None,
                                               "transport": "httpx"}),
             prefix + "README.md": b"# A session\n"}
    for name, record in calls().items():
        files[prefix + name] = as_json(record)
    files[prefix + "integration_GetMap_20261020-090005.000000_response.txt"] = MAP
    if dicom:
        files[CT_FILE] = ct_slice()
    return files


def manifest(files, **changes):
    value = {"format": "maprt-pack/1", "name": "test-pack", "description": "for the tests",
             "created": "2026-10-28T00:00:00+00:00", "anonymizer": "hand-written", "anonymized": True,
             "patient": {"id": PATIENT, "name": "PHANTOM^TEST"},
             "appliance": {"server": SERVER, "apiVersion": "2.0.0"},
             "files": {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())}}
    value.update(changes)
    return value


def write_pack(target, files, *, as_zip=True, **manifest_changes):
    """Write ``files`` plus their manifest to ``target`` (a ``.zip`` or a folder). Returns ``target``."""
    tree = {"manifest.json": as_json(manifest(files, **manifest_changes)), **files}
    if as_zip:
        with zipfile.ZipFile(target, "w") as archive:
            for path, data in tree.items():
                archive.writestr(path, data)
    else:
        for path, data in tree.items():
            (target / path).parent.mkdir(parents=True, exist_ok=True)
            (target / path).write_bytes(data)
    return target


def as_json(value):
    return (json.dumps(value, indent=2) + "\n").encode()


def edit_record(files, local_name, **changes):
    """Change keys of one session record in ``files`` (``None`` removes a key)."""
    path = f"sessions/{SESSION}/{local_name}"
    record = json.loads(files[path])
    for key, value in changes.items():
        if value is None:
            record.pop(key, None)
        else:
            record[key] = value
    files[path] = as_json(record)


def ct_slice(*, patient_id=PATIENT, removed="YES"):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientID = patient_id
    ds.PatientName = "PHANTOM^TEST"
    if removed is not None:
        ds.PatientIdentityRemoved = removed
    buffer = io.BytesIO()
    pydicom.dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()
