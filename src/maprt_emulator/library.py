"""The loaded packs, indexed by what a route looks up: rooms, patients, surfaces, maps, verdicts, errors.

:meth:`PackLibrary.load` finds and loads the packs (:mod:`maprt_emulator.packs`) and builds every
index once, at startup, so a route only looks an answer up. Its rules, in the words of the Phase 6
decisions:

* **A pack that fails a check is refused when named and skipped when scanned.** Named (``--pack``):
  any failure refuses startup, listing every finding of every failing pack. Scanned (found in
  ``--packs-dir`` with no ``--pack``): a failing pack is skipped entirely, and reported; **every**
  scanned pack failing refuses startup, since serving nothing from a packs folder would hide the
  problem.
* **Problems between packs always refuse startup**, named or scanned: two packs of one name, a
  room GUID with different fields, a surface with different bytes, one patient's surface with two
  timestamps, the newest appliance with two API versions. Neither pack is wrong on its own, so
  skipping one would be a guess; the error names both, and ``--pack`` chooses.
* Every refusal ends with how to start without packs (:data:`NO_PACKS_HINT`).
* **The appliance served is the newest** among the packs (``Saturn/<numbers>`` compared as
  numbers), with its own pack's API version, and a notice when the packs name more than one.
* **A surface listed but never fetched** (in any pack) is in :attr:`PackLibrary.unfetched`.
* **Maps, verdicts and errors are keyed by the request** (:func:`request_key`): the body parsed with
  the route's own model, so only fields the emulator knows count, ``20`` matches ``20.0``,
  ``PatientPosition: 1`` matches ``"HFS"``, and key order doesn't matter. The first recorded reply
  for a key wins; repeats are counted.
* **A recorded success beats a recorded error** for the same thing (a patient, a surface, a map
  request), whatever their order. Errors that depend on the caller rather than the data (``AuthErrorCodes``,
  and any non-200) aren't indexed: the emulator answers those itself. Room lookups by machine name
  aren't indexed at all: they're answered from the room union.
"""

import base64
import hashlib
import json
from dataclasses import dataclass, field

from pydantic import ValidationError

from maprt_emulator.packs import PackDiscoveryError, PackError, discover_packs, load_pack
from maprt_emulator.schemas import GetBeamDeliveryStatusRequest, GetMapRequest
from maprt_emulator.store import CatalogError

NO_PACKS_HINT = ("To serve the built-in synthetic catalog instead, run with --no-packs "
                 "(or set MAPRT_EMULATOR_NO_PACKS=1).")

ROOMS = "/integration/rooms"
ROOM_LOOKUP = "/integration/rooms/{machineName}"
PATIENT_SURFACES = "/integration/patients/{patientId}/surfaces"
SURFACE = "/integration/surfaces/{surfaceId}"
GETMAP = "/integration/GetMap"
BEAMS = "/integration/GetBeamDeliveryStatus"
PING = "/integration/ping"

_MODELS = {GETMAP: GetMapRequest, BEAMS: GetBeamDeliveryStatusRequest}


class PacksRefused(CatalogError):
    """Startup is refused: a named pack failed, every scanned pack failed, or two packs disagree."""

    def __init__(self, message, failures=()):
        #: The :class:`PackError` of each pack that failed its checks, if that's the reason.
        self.failures = tuple(failures)
        super().__init__(f"{message}\n{NO_PACKS_HINT}")


class PackConflictError(PacksRefused):
    """Two packs (or two sessions) disagree about one thing. Neither is wrong on its own."""


def load_library(settings):
    """The :class:`PackLibrary` the settings ask for, or ``None`` to serve the catalog instead
    (``no_packs``, or no ``packs_dir``)."""
    if settings.no_packs:
        return None
    if settings.packs_dir is None:
        if settings.packs:
            raise CatalogError(f"Packs were named ({', '.join(settings.packs)}) but no packs folder was given "
                               f"(--packs-dir).")
        return None
    return PackLibrary.load(settings.packs_dir, settings.packs)


def serves_packs(app):
    """True when the app answers from packs: a library with at least one pack (an empty packs
    folder serves the catalog, as no packs do)."""
    library = getattr(app.state, "library", None)
    return library is not None and bool(library.packs)


# -- the request key ---------------------------------------------------------------------------

def request_key(model):
    """The key a parsed GetMap / GetBeamDeliveryStatus request is matched on: the fields the
    route's model knows (aliased, as on the wire), as compact JSON with sorted keys. ``-0.0`` is
    ``0.0`` (MapApp rounds a tiny negative isocenter coordinate to ``-0.0``)."""
    return json.dumps(_positive_zero(model.model_dump(by_alias=True, mode="json")),
                      sort_keys=True, separators=(",", ":"))


def recorded_key(route, body):
    """:func:`request_key` for a recorded request ``body`` to ``route`` (:data:`GETMAP` or
    :data:`BEAMS`), or ``None`` if the body isn't one the route would accept."""
    try:
        return request_key(_MODELS[route].model_validate(body))
    except ValidationError:
        return None


def _positive_zero(value):
    if isinstance(value, float):
        return value + 0.0
    if isinstance(value, dict):
        return {key: _positive_zero(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_positive_zero(item) for item in value]
    return value


def route_of(path):
    """``(route template, its parameter)`` for a recorded path, or ``(None, None)`` for a path
    that isn't a MapRT route."""
    parts = path.split("/")
    if path in (PING, ROOMS, GETMAP, BEAMS):
        return path, None
    if len(parts) == 4 and parts[2] == "rooms":
        return ROOM_LOOKUP, parts[3]
    if len(parts) == 5 and parts[2] == "patients" and parts[4] == "surfaces":
        return PATIENT_SURFACES, parts[3]
    if len(parts) == 4 and parts[2] == "surfaces":
        return SURFACE, parts[3]
    return None, None


# -- the library -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Skipped:
    """A scanned pack that failed its checks and isn't served."""

    path: object
    name: str | None
    findings: tuple

    def __str__(self):
        label = f"{self.name} ({self.path})" if self.name else str(self.path)
        return f"{label}: " + "; ".join(map(str, self.findings))


@dataclass(frozen=True)
class Unfetched:
    """A surface a patient's list names, with no recorded mesh in any pack."""

    surface_id: str
    patient_id: str
    packs: tuple

    def __str__(self):
        return (f"{', '.join(self.packs)}: patient {self.patient_id}, surface {self.surface_id} listed but never "
                f"fetched; fetches answer SurfaceNotFound")


@dataclass
class PackLibrary:
    packs: tuple = ()
    skipped: tuple = ()
    #: GUID → the room as recorded, in first-seen order.
    rooms: dict = field(default_factory=dict)
    #: Patient ID → their surface list (dicts, as recorded), the union across packs, first-seen order.
    patients: dict = field(default_factory=dict)
    #: Patient ID → their first recorded successful list reply, served with ``data`` replaced by the
    #: union, so the envelope keeps the recorded keys and order.
    patient_replies: dict = field(default_factory=dict)
    #: Surface ID → the :class:`Recorded` successful fetch.
    surfaces: dict = field(default_factory=dict)
    #: Surface ID → :class:`Unfetched`.
    unfetched: dict = field(default_factory=dict)
    #: :func:`request_key` → the :class:`Recorded` GetMap that returned map text.
    maps: dict = field(default_factory=dict)
    #: :func:`request_key` → the :class:`Recorded` successful GetBeamDeliveryStatus.
    verdicts: dict = field(default_factory=dict)
    #: ``(route template, parameter or request key)`` → the :class:`Recorded` error reply.
    errors: dict = field(default_factory=dict)
    #: ``{"server", "apiVersion"}`` presented to clients, or ``None`` with no packs.
    appliance: dict | None = None
    #: ``[{"server", "apiVersion", "packs": [...]}]``, newest first.
    appliance_versions: list = field(default_factory=list)
    #: What startup should warn about once: more than one appliance version.
    warnings: list = field(default_factory=list)
    #: What startup should report once: repeats, unanswered calls, unfetched surfaces.
    notices: list = field(default_factory=list)

    @classmethod
    def load(cls, packs_dir, names=()):
        """Find, load and index the packs in ``packs_dir`` (only ``names``, if given). Raises
        :class:`PacksRefused` as the module docstring says."""
        try:
            candidates = discover_packs(packs_dir, names)
        except PackDiscoveryError as exc:
            raise PacksRefused(str(exc)) from exc

        loaded, failures = [], []
        for candidate in candidates:
            try:
                loaded.append(load_pack(candidate.path))
            except PackError as exc:
                failures.append(exc)
        if failures and names:
            raise PacksRefused(_failed("named", failures), failures)
        if failures and not loaded:
            raise PacksRefused(_failed("scanned", failures), failures)
        skipped = tuple(Skipped(exc.path, exc.name, exc.findings) for exc in failures)
        return cls.build(loaded, skipped)

    @classmethod
    def build(cls, packs, skipped=()):
        """Index already-loaded packs (in the order given). Raises :class:`PackConflictError`."""
        library = cls(tuple(packs), tuple(skipped))
        library._index()
        return library

    # -- building -----------------------------------------------------------------------------

    def _index(self):
        sources = {}            # (index, key) → the Recorded that gave it, for conflict messages
        repeats = unmatchable = 0
        recorded_errors = []
        for pack in self.packs:
            for exchange in pack.exchanges:
                if exchange.transport_failure:
                    continue
                route, param = route_of(exchange.path)
                body, text = exchange.body, exchange.text
                succeeded = exchange.status == 200 and (
                    text is not None or (isinstance(body, dict) and body.get("success") is True))
                if not succeeded:
                    recorded_errors.append((route, param, exchange))
                elif route == ROOMS:
                    for room in body.get("data") or ():
                        self._add_room(room, exchange, sources)
                elif route == PATIENT_SURFACES:
                    self._add_patient(param, body.get("data") or [], exchange, sources)
                elif route == SURFACE:
                    self._add_surface(param, exchange, sources)
                elif (route == GETMAP and text is not None) or route == BEAMS:
                    key = recorded_key(route, exchange.request_body)
                    index = self.maps if route == GETMAP else self.verdicts
                    if key is None:
                        unmatchable += 1
                    elif key in index:
                        repeats += 1
                    else:
                        index[key] = exchange
        for route, param, exchange in recorded_errors:
            self._add_error(route, param, exchange)

        for patient_id, listed in self.patients.items():
            for surface in listed:
                if surface["id"] not in self.surfaces:
                    packs = tuple(dict.fromkeys(e.pack for e in sources[("patient-surface", patient_id, surface["id"])]))
                    self.unfetched.setdefault(surface["id"], Unfetched(surface["id"], patient_id, packs))

        self._choose_appliance()
        if repeats:
            self.notices.append(f"{repeats} recorded map or beam-check repeat(s) of an earlier request; "
                                f"the first recorded reply is served")
        if unmatchable:
            self.notices.append(f"{unmatchable} recorded map or beam-check request(s) aren't ones the route would "
                                f"accept, so no request can match them; they're not served")
        failures = sum(pack.transport_failures for pack in self.packs)
        if failures:
            self.notices.append(f"{failures} recorded call(s) never got a reply; they're not served")
        self.notices += [str(entry) for entry in self.unfetched.values()]

    def _add_room(self, room, exchange, sources):
        guid = room.get("id")
        if guid in self.rooms:
            if self.rooms[guid] != room:
                raise PackConflictError(
                    f"Room {guid} is recorded with different fields by {_where(sources[('room', guid)][0])} "
                    f"and {_where(exchange)}.")
            return
        self.rooms[guid] = room
        sources[("room", guid)] = [exchange]

    def _add_patient(self, patient_id, listed, exchange, sources):
        self.patient_replies.setdefault(patient_id, exchange)
        surfaces = self.patients.setdefault(patient_id, [])
        known = {surface["id"]: surface for surface in surfaces}
        for surface in listed:
            key = ("patient-surface", patient_id, surface.get("id"))
            if surface.get("id") in known:
                if known[surface["id"]] != surface:
                    raise PackConflictError(
                        f"Surface {surface['id']} of patient {patient_id} is listed with different fields by "
                        f"{_where(sources[key][0])} and {_where(exchange)}.")
                sources[key].append(exchange)
                continue
            surfaces.append(surface)
            known[surface["id"]] = surface
            sources[key] = [exchange]

    def _add_surface(self, surface_id, exchange, sources):
        if surface_id in self.surfaces:
            if _compact(self.surfaces[surface_id].body) != _compact(exchange.body):
                raise PackConflictError(
                    f"Surface {surface_id} is recorded with different bytes by "
                    f"{_where(self.surfaces[surface_id])} and {_where(exchange)}.")
            return
        self.surfaces[surface_id] = exchange

    def _add_error(self, route, param, exchange):
        body = exchange.body
        if route in (None, ROOM_LOOKUP, PING, ROOMS) or exchange.status != 200 or not isinstance(body, dict):
            return
        if body.get("errorCodeType") == "AuthErrorCodes":
            return
        if route in (GETMAP, BEAMS):
            param = recorded_key(route, exchange.request_body)
            if param is None or param in (self.maps if route == GETMAP else self.verdicts):
                return
        elif param in (self.patients if route == PATIENT_SURFACES else self.surfaces):
            return
        self.errors.setdefault((route, param), exchange)

    def _choose_appliance(self):
        if not self.packs:
            return
        by_pair = {}
        for pack in self.packs:
            by_pair.setdefault((pack.server_version, pack.server, pack.api_version), []).append(pack.name)
        ordered = sorted(by_pair.items(), key=lambda item: item[0][0], reverse=True)
        self.appliance_versions = [{"server": server, "apiVersion": api, "packs": names}
                                   for (_version, server, api), names in ordered]
        top = ordered[0][0][0]
        newest = [entry for ((version, _server, _api), _names), entry in zip(ordered, self.appliance_versions)
                  if version == top]
        if len(newest) > 1:
            listed = "; ".join(f"API {entry['apiVersion']}: {', '.join(entry['packs'])}" for entry in newest)
            raise PackConflictError(
                f"The newest appliance, {newest[0]['server']}, is recorded with more than one API version ({listed}).")
        self.appliance = {"server": newest[0]["server"], "apiVersion": newest[0]["apiVersion"]}
        if len(self.appliance_versions) > 1:
            older = " ".join(f"{entry['server']}: {', '.join(entry['packs'])}." for entry in self.appliance_versions[1:])
            self.warnings.append(f"Packs were recorded on {len(self.appliance_versions)} appliance versions; "
                                   f"serving as {self.appliance['server']} (API {self.appliance['apiVersion']}), "
                                   f"the newest. {older}")

    # -- answering ----------------------------------------------------------------------------

    def rooms_json(self):
        """``GET /rooms``: the union, each room exactly as recorded."""
        return list(self.rooms.values())

    def patient_reply(self, patient_id):
        """``GET /patients/{id}/surfaces`` for a patient with a recorded list: the first recorded
        reply, its ``data`` the union of every recorded list."""
        return {**self.patient_replies[patient_id].body, "data": self.patients[patient_id]}

    def random_map(self, high_resolution, key, seed=0):
        """A recorded map of the requested resolution for a request with none of its own, or
        ``None`` if no map of that resolution was recorded. **Not this request's data**: a map
        recorded for some other surface, room or parameters.

        Chosen from the maps of that resolution by a hash of ``seed`` and the request's key, so the
        same request always gets the same map (a replay, or a student re-running a cell, sees one
        answer) while different requests spread over the recordings."""
        pool = [k for k in sorted(self.maps) if json.loads(k).get("HighResolution") is high_resolution]
        if not pool:
            return None
        digest = hashlib.sha256(f"{seed}:{key}".encode()).digest()
        return self.maps[pool[int.from_bytes(digest[:8], "big") % len(pool)]]

    def surface_obj(self, surface_id):
        """The fetched surface's OBJ bytes (decoded from its recorded data URI), or ``None``."""
        fetched = self.surfaces.get(surface_id)
        if fetched is None:
            return None
        return base64.b64decode(str(fetched.body.get("data", "")).rpartition(",")[2])

    def room_ids_by_machine_name(self, machine_name):
        """``GET /rooms/{machineName}``, from the union (never a recorded lookup): every GUID whose
        machine name is exactly this one, trailing spaces and case included."""
        return [guid for guid, room in self.rooms.items() if room.get("machineName") == machine_name]

    # -- reporting ----------------------------------------------------------------------------

    def report(self):
        """The ``packs`` block of ``server.json``: what's served, what was skipped and why."""
        return {
            "summary": self.summary(),
            "loaded": [{"name": pack.name, "path": str(pack.path), "patient": pack.patient_id,
                        "appliance": {"server": pack.server, "apiVersion": pack.api_version}}
                       for pack in self.packs],
            "skipped": [{"path": str(entry.path), "name": entry.name, "findings": [str(f) for f in entry.findings]}
                        for entry in self.skipped],
            "appliance": self.appliance,
            "applianceVersions": self.appliance_versions,
            "unfetched": [{"surface": entry.surface_id, "patient": entry.patient_id, "packs": list(entry.packs)}
                          for entry in self.unfetched.values()],
            "warnings": list(self.warnings),
            "notices": list(self.notices),
        }

    def summary(self):
        """The banner line."""
        line = (f"{_count(len(self.packs), 'pack')}, {_count(len(self.patients), 'patient')}, "
                f"{_count(len(self.surfaces), 'surface')}, {_count(len(self.rooms), 'room')}, "
                f"{_count(len(self.maps), 'map')}, {_count(len(self.verdicts), 'beam check')}")
        if self.unfetched:
            line += (f"; {_count(len(self.unfetched), 'listed surface')} "
                     f"{'has' if len(self.unfetched) == 1 else 'have'} no recorded mesh")
        if self.skipped:
            line += f"; skipped {_count(len(self.skipped), 'pack')} that failed its checks (see the log)"
        return line


def _failed(how, failures):
    which = "A named pack" if how == "named" and len(failures) == 1 else (
        "Named packs" if how == "named" else "Every pack in the packs folder")
    return f"{which} failed its checks, so nothing is served:\n" + "\n".join(map(str, failures))


def _where(exchange):
    return f"pack {exchange.pack} (session {exchange.session})"


def _compact(body):
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _count(n, noun):
    return f"{n} {noun}{'' if n == 1 else 's'}"
