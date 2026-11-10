"""Reading session packs: the format, the load-time checks, and the recorded calls they hold.

A pack is a ``.zip`` (or, while developing, a folder with a ``manifest.json``) written by
``maprt-pack``; the format is ``docs/PACK_FORMAT.md``. This module reads one into a :class:`Pack`
and refuses it — with every finding at once, so one run shows everything to fix — when it fails a
check. It changes no route: what the emulator serves from packs is built on top of this.

**The checks repeat ``maprt_pack.verify.check_pack``'s**, because the emulator can't depend on a
workspace package (it's published on its own): the format, ``anonymized: true``, every sha256, no
URL with a host, no ``host`` header, ``api_url`` null, only the manifest's patient in a
``/patients/{id}`` URL, every ``body_ref`` resolvable to a body that isn't itself a reference, and
every DICOM file marked de-identified with the manifest's patient ID. A workspace contract test
keeps the two in step. **Plus the emulator's own:** each session's recording format, each record's
file name and its pair, and an ``appliance.server`` that reads as ``Saturn/<numbers>`` (the
emulator compares those versions when packs from several appliances are loaded). The optional
``source`` block (phantom or patient, and the labels the person packing accepted) is checked when
present; a pack without it loads as "source unknown".

The DICOM is read only to check it, then dropped: the emulator doesn't serve it. Everything else
is kept in memory.
"""

import datetime as dt
import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pydicom
from pydicom.errors import InvalidDicomError

from maprt_emulator.store import CatalogError

PACK_FORMAT = "maprt-pack/1"

#: The recording format a pack's sessions are in. A ``session.json`` without ``format`` was
#: written before the field existed and is read as this version.
RECORDING_FORMAT = "maprt-recording/1"

#: ``<endpoint>_<timestamp>[_<n>]_<request|response>.<json|txt>``, as the recorder names files.
_RECORD_NAME = re.compile(
    r"^(?P<stem>.+_\d{8}-\d{6}\.\d{6}(?:_\d+)?)_(?P<kind>request|response)\.(?P<ext>json|txt)$")

#: What a pack can be built from: the manifest's ``source.kind``.
SOURCE_KINDS = ("phantom", "patient")

#: ``Saturn/1.3.217.2``: the product, then only numbers.
_SERVER = re.compile(r"^Saturn/(\d+(?:\.\d+)*)$")


class PackError(CatalogError):
    """A pack can't be served. Raised at startup, never mid-request; the message lists every finding."""

    def __init__(self, path, findings, name=None):
        self.path = Path(path)
        self.name = name
        self.findings = tuple(findings)
        label = f"{name} ({self.path})" if name else str(self.path)
        listed = "\n".join(f"  - {finding}" for finding in self.findings)
        super().__init__(f"Pack {label} failed its checks:\n{listed}")


class PackDiscoveryError(CatalogError):
    """The packs asked for can't be told apart or found: a duplicate name, or a name that matches no pack."""


@dataclass(frozen=True)
class Finding:
    file: str
    problem: str

    def __str__(self):
        return f"{self.file}: {self.problem}"


@dataclass(frozen=True)
class Recorded:
    """One recorded call, with its reply resolved: ``body`` for JSON, ``text`` for a map, neither for
    a call that never got one. The two records are kept whole for logging and timing; only the
    reply's ``body`` or ``text`` is ever served."""

    pack: str
    session: str
    stem: str
    method: str
    #: The request URL's path, unquoted (``/integration/patients/ANON-1/surfaces``).
    path: str
    request_body: object
    request: dict = field(repr=False)
    response: dict | None = field(repr=False)
    body: object = None
    text: bytes | None = None

    @property
    def status(self):
        return None if self.response is None else self.response.get("status")

    @property
    def transport_failure(self):
        """No reply crossed the wire: no response record, or one that records only a transport error."""
        return self.response is None or self.status is None or bool(self.response.get("error"))


@dataclass(frozen=True)
class RecordedSession:
    id: str
    info: dict = field(repr=False)
    #: In the order they were sent.
    exchanges: tuple = ()


@dataclass(frozen=True)
class Pack:
    name: str
    path: Path
    manifest: dict = field(repr=False)
    patient_id: str = ""
    #: ``Saturn/1.3.217.2`` as recorded, and as numbers for comparing appliances.
    server: str = ""
    server_version: tuple = ()
    api_version: str = ""
    #: In the order they started.
    sessions: tuple = ()
    #: The manifest's ``source`` block (``kind``, ``reviewed``, ``labelsAccepted``), or ``None``
    #: when the pack doesn't say what it was built from.
    source: dict | None = None

    @property
    def exchanges(self):
        return tuple(exchange for session in self.sessions for exchange in session.exchanges)

    @property
    def transport_failures(self):
        """Calls that never got a reply. Counted, never served."""
        return sum(1 for exchange in self.exchanges if exchange.transport_failure)


# -- finding packs -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """A pack found in a packs folder, before it's loaded. ``name`` is its manifest's, or ``None``
    when the manifest can't be read (the pack then fails its checks, named by its path)."""

    path: Path
    name: str | None


def discover_packs(packs_dir, names=()):
    """The packs directly inside ``packs_dir`` (every ``*.zip`` and every folder with a
    ``manifest.json``; not recursive), in path order. With ``names``, only those, in that order.

    A pack's name is its manifest's ``name``, not its file name. Raises
    :class:`PackDiscoveryError` when two packs share a name, or a name matches no pack.
    """
    packs_dir = Path(packs_dir)
    if not packs_dir.is_dir():
        raise PackDiscoveryError(f"No packs folder at {packs_dir}.")
    found = [Candidate(path, _manifest_name(path)) for path in sorted(packs_dir.iterdir())
             if (path.is_file() and path.suffix.lower() == ".zip")
             or (path.is_dir() and (path / "manifest.json").is_file())]

    by_name = {}
    for candidate in found:
        if candidate.name is not None:
            by_name.setdefault(candidate.name, []).append(candidate.path)
    duplicates = {name: paths for name, paths in by_name.items() if len(paths) > 1}
    if duplicates:
        listed = "\n".join(f"  - {name}: {', '.join(map(str, paths))}" for name, paths in sorted(duplicates.items()))
        raise PackDiscoveryError(f"More than one pack has the same name, so they can't be told apart:\n{listed}")

    if not names:
        return found
    missing = [name for name in names if name not in by_name]
    if missing:
        known = ", ".join(sorted(by_name)) or "none"
        raise PackDiscoveryError(f"No pack in {packs_dir} is named {', '.join(missing)} (the packs there: {known}).")
    return [Candidate(by_name[name][0], name) for name in dict.fromkeys(names)]


def _manifest_name(path):
    try:
        if path.is_dir():
            data = (path / "manifest.json").read_bytes()
        else:
            with zipfile.ZipFile(path) as archive:
                data = archive.read("manifest.json")
        name = json.loads(data).get("name")
    except (OSError, KeyError, ValueError, AttributeError, zipfile.BadZipFile):
        return None
    return name if isinstance(name, str) and name.strip() else None


# -- loading -----------------------------------------------------------------------------------

def read_tree(source):
    """``{relative path: bytes}`` for a pack folder or ``.zip``. A folder's hidden files (a Mac's
    ``.DS_Store``) are left out: they were never part of the pack."""
    source = Path(source)
    if source.is_dir():
        return {path.relative_to(source).as_posix(): path.read_bytes()
                for path in sorted(source.rglob("*"))
                if path.is_file() and not any(part.startswith(".") for part in path.relative_to(source).parts)}
    with zipfile.ZipFile(source) as archive:
        return {name: archive.read(name) for name in sorted(archive.namelist()) if not name.endswith("/")}


def load_pack(path):
    """Read and check the pack at ``path``. Returns a :class:`Pack`, or raises :class:`PackError`
    listing every finding."""
    path = Path(path)
    try:
        tree = read_tree(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise PackError(path, [Finding(path.name, f"can't be read ({exc})")]) from exc

    findings = []

    def fail(file, problem):
        findings.append(Finding(file, problem))

    manifest = _read_json(tree, "manifest.json", fail) if "manifest.json" in tree else None
    if manifest is None:
        if "manifest.json" not in tree:
            fail("manifest.json", "missing")
        raise PackError(path, findings)
    if not isinstance(manifest, dict):
        raise PackError(path, [Finding("manifest.json", "isn't a JSON object")])

    name = manifest.get("name") if isinstance(manifest.get("name"), str) and manifest["name"].strip() else None
    if name is None:
        fail("manifest.json", "name is missing")
    if manifest.get("format") != PACK_FORMAT:
        fail("manifest.json", f"format is not {PACK_FORMAT}")
    if manifest.get("anonymized") is not True:
        fail("manifest.json", "not marked anonymized")
    patient_id = (manifest.get("patient") or {}).get("id")
    if not isinstance(patient_id, str) or not patient_id:
        fail("manifest.json", "patient.id is missing")
    server, server_version, api_version = _check_appliance(manifest.get("appliance"), fail)
    source = _check_source(manifest.get("source"), fail)

    _check_files(tree, manifest.get("files"), fail)
    for file, data in tree.items():
        if file.endswith(".dcm"):
            _check_dicom(file, data, patient_id, fail)

    sessions = []
    for session_id in _session_ids(tree):
        session = _read_session(tree, session_id, name or path.name, patient_id, fail)
        if session is not None:
            sessions.append(session)
    if not sessions:
        fail("sessions/", "the pack holds no session")

    if findings:
        raise PackError(path, findings, name)
    sessions.sort(key=lambda s: (str(s.info.get("started", "")), s.id))
    return Pack(name, path, manifest, patient_id, server, server_version, api_version, tuple(sessions),
                source=source)


def appliance_version(server):
    """``(1, 3, 217, 2)`` for ``Saturn/1.3.217.2``, or ``None`` if it doesn't read that way."""
    match = _SERVER.match(server) if isinstance(server, str) else None
    return tuple(int(part) for part in match.group(1).split(".")) if match else None


def _check_appliance(appliance, fail):
    appliance = appliance if isinstance(appliance, dict) else {}
    server, api_version = appliance.get("server"), appliance.get("apiVersion")
    version = appliance_version(server)
    if version is None:
        fail("manifest.json", f"appliance.server {server!r} doesn't read as Saturn/<numbers>")
    if not isinstance(api_version, str) or not api_version:
        fail("manifest.json", f"appliance.apiVersion {api_version!r} isn't a version string")
    return server, version or (), api_version


def _check_source(source, fail):
    """The manifest's optional ``source`` block, checked; ``None`` when there's none ("source unknown")."""
    if source is None:
        return None
    if not isinstance(source, dict):
        fail("manifest.json", "source isn't a JSON object")
        return None
    problems = []
    if source.get("kind") not in SOURCE_KINDS:
        problems.append(f"source.kind {source.get('kind')!r} is not one of {', '.join(SOURCE_KINDS)}")
    try:
        dt.date.fromisoformat(source.get("reviewed"))
    except (TypeError, ValueError):
        problems.append(f"source.reviewed {source.get('reviewed')!r} isn't a YYYY-MM-DD date")
    labels = source.get("labelsAccepted")
    if not (isinstance(labels, dict) and all(isinstance(key, str) and isinstance(values, list)
                                             and all(isinstance(value, str) for value in values)
                                             for key, values in labels.items())):
        problems.append("source.labelsAccepted isn't a mapping of keywords to lists of text")
    for problem in problems:
        fail("manifest.json", problem)
    return None if problems else source


def _check_files(tree, listed, fail):
    if not isinstance(listed, dict):
        fail("manifest.json", "files is missing")
        return
    for file, data in tree.items():
        if file == "manifest.json":
            continue
        if file not in listed:
            fail(file, "not listed in the manifest's files")
        elif hashlib.sha256(data).hexdigest() != listed[file]:
            fail(file, "sha256 does not match the manifest")
    for file in listed:
        if file not in tree:
            fail(file, "listed in the manifest but missing from the pack")


def _check_dicom(file, data, patient_id, fail):
    try:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
    except (InvalidDicomError, ValueError, EOFError) as exc:
        fail(file, f"isn't a readable DICOM file ({exc})")
        return
    if ds.get("PatientIdentityRemoved") != "YES":
        fail(file, "PatientIdentityRemoved is not YES")
    if ds.get("PatientID") != patient_id:
        fail(file, "PatientID is not the manifest's patient")


def _session_ids(tree):
    return sorted({file.split("/")[1] for file in tree if file.startswith("sessions/") and file.count("/") >= 2})


def _read_session(tree, session_id, pack_name, patient_id, fail):
    prefix = f"sessions/{session_id}/"
    info = _read_json(tree, prefix + "session.json", fail) if prefix + "session.json" in tree else None
    if info is None:
        if prefix + "session.json" not in tree:
            fail(prefix + "session.json", "missing")
        return None
    if not isinstance(info, dict):
        fail(prefix + "session.json", "isn't a JSON object")
        return None
    if info.get("format", RECORDING_FORMAT) != RECORDING_FORMAT:
        fail(prefix + "session.json", f"format {info.get('format')!r} is not {RECORDING_FORMAT}")
    if info.get("api_url"):
        fail(prefix + "session.json", "api_url is set")

    requests, responses = {}, {}
    for file in tree:
        if not file.startswith(prefix) or "/" in file[len(prefix):]:
            continue
        local = file[len(prefix):]
        if local in ("session.json", "README.md"):
            continue
        match = _RECORD_NAME.match(local)
        if match is None:
            fail(file, "isn't a recording file name")
        elif match["kind"] == "request" and match["ext"] == "json":
            requests[match["stem"]] = file
        elif match["kind"] == "response" and match["ext"] == "json":
            responses[match["stem"]] = file
    for stem in sorted(set(responses) - set(requests)):
        fail(responses[stem], "a response with no request")

    exchanges = []
    for stem, file in sorted(requests.items()):
        request = _read_json(tree, file, fail)
        response = _read_json(tree, responses[stem], fail) if stem in responses else None
        if not isinstance(request, dict) or (stem in responses and not isinstance(response, dict)):
            continue
        _check_record(file, request, patient_id, fail)
        if response is not None:
            _check_record(responses[stem], response, patient_id, fail)
        body, text = _reply(tree, prefix, responses.get(stem), response, fail)
        exchanges.append(Recorded(
            pack=pack_name, session=session_id, stem=stem, method=str(request.get("method", "")),
            path=unquote(urlsplit(str(request.get("url", ""))).path), request_body=request.get("body"),
            request=request, response=response, body=body, text=text))
    exchanges.sort(key=lambda exchange: (str(exchange.request.get("sent", "")), exchange.stem))
    return RecordedSession(str(info.get("session") or session_id), info, tuple(exchanges))


def _check_record(file, record, patient_id, fail):
    url = record.get("url")
    if url is not None and not str(url).startswith("/"):
        fail(file, "url has a scheme or host")
    if record.get("api_url"):
        fail(file, "api_url is set")
    if url:
        parts = unquote(urlsplit(str(url)).path).split("/")
        if len(parts) >= 4 and parts[2] == "patients" and parts[3] != patient_id:
            fail(file, "url names another patient")
    if "host" in {str(key).lower() for key in (record.get("headers") or {})}:
        fail(file, "headers carry host")


def _reply(tree, prefix, file, response, fail):
    """``(body, text)`` for a response record, with ``body_file`` read and ``body_ref`` resolved."""
    if response is None:
        return None, None
    ref = response.get("body_ref")
    if ref is not None:
        target = _read_json(tree, ref, fail) if ref in tree else None
        if not isinstance(target, dict):
            if ref not in tree:
                fail(file, "body_ref points at no file")
            return None, None
        if "body_ref" in target:
            fail(file, "body_ref points at another reference")
            return None, None
        # A reference may cross sessions, so a target's body_file sits beside the target.
        return _body(tree, ref.rpartition("/")[0] + "/", ref, target, fail)
    return _body(tree, prefix, file, response, fail)


def _body(tree, prefix, file, record, fail):
    name = record.get("body_file")
    if name:
        text = tree.get(prefix + name)
        if text is None:
            fail(file, f"body_file {name} is missing")
        return None, text
    return record.get("body"), None


def _read_json(tree, file, fail):
    try:
        return json.loads(tree[file])
    except ValueError as exc:
        fail(file, f"isn't valid JSON ({exc})")
        return None
