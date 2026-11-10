"""The inventory page: what this emulator serves, and what a call can reach.

Served at ``GET /inventory`` (HTML) and ``GET /inventory.md`` (Markdown), and printed by
``maprt-emulator inventory``. It's a page for people, like ``/docs``: outside ``/integration``, no
token, and no MapRT client calls it (a real appliance has no such page). Nothing on it changes any
API reply.

With packs loaded it lists every pack served (and every pack skipped, with why), then what a call
can reach: the rooms and what a lookup by machine name answers, each patient's surfaces (fetched,
or listed but never fetched), every recorded GetMap and beam check with a **copy-ready request
body** of the exact recorded values, the recorded errors, and what a request that wasn't recorded
gets. Without packs it lists the catalog.

The page is built once as a small document (headings, paragraphs, lists, tables, code) and written
out as either Markdown or HTML, so the two say the same thing and no Markdown library is needed.
"""

import html
import json
import re

from maprt_emulator.library import BEAMS, GETMAP, PATIENT_SURFACES, SURFACE, serves_packs

TITLE = "MapRT Emulator inventory"


# -- the document ------------------------------------------------------------------------------

def _h(level, text):
    return ("h", level, text)


def _p(text):
    return ("p", text)


def _ul(items):
    return ("ul", list(items))


def _table(headers, rows):
    return ("table", list(headers), [list(row) for row in rows])


def _code(text):
    return ("code", text)


def build(app):
    """The inventory of what ``app`` serves, as a list of blocks."""
    settings = app.state.settings
    library = app.state.library
    blocks = [_h(1, TITLE),
              _p("What this emulator serves, and what a call can reach. A page for people: a real MapRT "
                 "appliance has none, and no client should call it.")]
    if serves_packs(app):
        blocks += _packs(app, library)
    else:
        if settings.no_packs:
            source = "Packs are off (`--no-packs`): serving the catalog."
        elif library is not None:
            source = f"No packs in `{settings.packs_dir}`: serving the catalog."
        else:
            source = "No packs folder (`--packs-dir`): serving the catalog."
        blocks += [_p(source), *_catalog(app)]
    return blocks


def _packs(app, library):
    settings = app.state.settings
    named = f", only {', '.join(f'`{name}`' for name in settings.packs)}" if settings.packs else ""
    appliance = library.appliance
    blocks = [_p(f"Serving packs from `{settings.packs_dir}`{named}: {library.summary()}."),
              _p(f"Presented as appliance **{appliance['server']}**, API **{appliance['apiVersion']}** "
                 f"(the `Server` header and the ping reply).")]
    blocks += [_p(f"**Warning:** {warning}") for warning in library.warnings]
    if library.skipped:
        blocks += [_h(2, "Skipped packs"),
                   _p("Found in the packs folder, but failed their checks, so nothing of them is served."),
                   _ul(f"`{entry.name or entry.path}` (`{entry.path}`): " + "; ".join(map(str, entry.findings))
                       for entry in library.skipped)]

    blocks.append(_h(2, "Packs"))
    for pack in library.packs:
        description = pack.manifest.get("description") or ""
        blocks += [_h(3, pack.name),
                   _ul([*([description] if description else []),
                        f"Patient `{pack.patient_id}`",
                        _source(pack.source),
                        f"Recorded on {pack.server}, API {pack.api_version}",
                        f"File `{pack.path}`"]),
                   _table(["Session", "Started", "Transport", "Calls"],
                          [[f"`{session.id}`", str(session.info.get("started", "")),
                            str(session.info.get("transport", "")), str(len(session.exchanges))]
                           for session in pack.sessions])]

    blocks += _rooms(library) + _patients(library) + _maps(library) + _beams(library) + _errors(library)
    blocks += _not_recorded()
    return blocks


def _source(source):
    """What the pack says it was built from, as the person packing declared it."""
    if source is None:
        return "Source unknown (the manifest doesn't say)"
    labels = source["labelsAccepted"]
    count = sum(len(values) for values in labels.values())
    accepted = (f"{count} label{'s' if count != 1 else ''} accepted as written ({', '.join(labels)})"
                if count else "no labels to review")
    if source["kind"] == "patient":
        return f"Source: **patient dataset**, labels reviewed {source['reviewed']}; {accepted}"
    return f"Source: phantom, saved {source['reviewed']}; {accepted}"


def _rooms(library):
    by_name = {}
    for guid, room in library.rooms.items():
        by_name.setdefault(room.get("machineName"), []).append(guid)

    def lookup(name):
        if name == "":
            return "— (an empty name is the list route)"
        guids = by_name[name]
        return f"`{guids[0]}`" if len(guids) == 1 else f"`DuplicateKey` ({len(guids)} rooms)"

    return [_h(2, "Rooms"),
            _p("`GET /integration/rooms` lists these, as recorded. `GET /integration/rooms/{machineName}` "
               "is answered from this list: one match is its GUID, several are `DuplicateKey`, none is the "
               "all-zeros GUID. A machine name is matched exactly, trailing spaces and case included."),
            _table(["Name", "Machine name", "Room GUID", "Lookup by machine name answers"],
                   [[_text(room.get("name")), f"`{json.dumps(room.get('machineName'))}`", f"`{guid}`",
                     lookup(room.get("machineName"))] for guid, room in library.rooms.items()])]


def _patients(library):
    blocks = [_h(2, "Patients and surfaces")]
    if not library.patients:
        return [*blocks, _p("No surface list was recorded.")]
    for patient_id, surfaces in library.patients.items():
        rows = []
        for surface in surfaces:
            fetched = surface["id"] in library.surfaces
            rows.append([f"`{surface['id']}`", _text(surface.get("timeStamp")), _text(surface.get("timeZone")),
                         "fetched" if fetched else "**listed but never fetched**: answers `SurfaceNotFound`"])
        blocks += [_h(3, f"Patient `{patient_id}`"),
                   _p(f"`GET /integration/patients/{patient_id}/surfaces`"),
                   _table(["Surface", "timeStamp", "timeZone", "GET /integration/surfaces/{id}"], rows)]
    return blocks


def _maps(library):
    blocks = [_h(2, "GetMap: recorded requests"),
              _p("Each of these is answered with the map recorded for it, byte for byte. Send the body as "
                 "it is (or with the same values: `20` and `20.0` match, key order doesn't matter).")]
    if not library.maps:
        return [*blocks, _p("No map was recorded.")]
    for n, exchange in enumerate(library.maps.values(), 1):
        body = exchange.request_body
        header = (exchange.text or b"").split(b"\r\n", 1)[0].decode("latin-1")
        blocks += [_h(3, f"Map {n}: {_room_name(library, body)}, "
                         f"{'high' if body.get('HighResolution') else 'low'} resolution"),
                   _ul([f"Surface `{body.get('PatientSurfaceId')}`, room `{body.get('TreatmentRoomId')}`",
                        _setup(body),
                        f"Map `{header}` (couch × gantry samples), from pack `{exchange.pack}`"]),
                   _code(json.dumps(body, indent=2))]
    return blocks


def _beams(library):
    blocks = [_h(2, "GetBeamDeliveryStatus: recorded requests"),
              _p("Each of these is answered with the verdict recorded for it.")]
    if not library.verdicts:
        return [*blocks, _p("No beam check was recorded.")]
    for n, exchange in enumerate(library.verdicts.values(), 1):
        body = exchange.request_body
        statuses = ((exchange.body or {}).get("data") or {}).get("statuses") or []
        unsafe = [s.get("beamNumber") for s in statuses if not s.get("isSafe")]
        verdict = f"{len(statuses)} beams, " + (f"unsafe: {', '.join(map(str, unsafe))}" if unsafe else "all safe")
        points = sum(len(beam.get("controlPoints") or []) for beam in body.get("BeamList") or [])
        blocks += [_h(3, f"Beam check {n}: {_room_name(library, body)}"),
                   _ul([f"Surface `{body.get('PatientSurfaceId')}`, room `{body.get('TreatmentRoomId')}`",
                        _setup(body),
                        (f"{len(body.get('BeamList') or [])} beams, {points} control points; recorded verdict: "
                         f"{verdict}; from pack `{exchange.pack}`")]),
                   _code(json.dumps(body, separators=(",", ":")))]
    return blocks


def _errors(library):
    if not library.errors:
        return []
    rows = []
    for (route, param), exchange in library.errors.items():
        target = f"`{param}`" if route in (PATIENT_SURFACES, SURFACE) else "the request recorded with it"
        reply = f"`{exchange.body.get('errorCode')}` ({exchange.body.get('errorCodeType')})"
        rows.append([f"`{route}`", target, reply, f"`{exchange.pack}`"])
    return [_h(2, "Recorded errors"),
            _p("Served for the request they answered (a success recorded anywhere for the same thing wins)."),
            _table(["Route", "For", "Recorded reply", "Pack"], rows)]


def _not_recorded():
    return [_h(2, "A request that wasn't recorded"),
            _ul([(f"`{GETMAP}`: a map recorded for another request of the same resolution (real appliance output, "
                  "but **not this request's data**; the same request always gets the same one), else a computed "
                  "(synthetic) map. On a surface listed but never fetched: `SurfaceNotFound`."),
                 (f"`{BEAMS}`: derived from the high-resolution map recorded for the same surface, room, position, "
                  "buffers and isocenter, when every control point is on it; else computed (synthetic). On a "
                  "surface listed but never fetched: `SurfaceNotFound`."),
                 ("An unknown patient: `PatientNotFoundInDatabase`. An unknown surface: `SurfaceNotFound`. A room "
                  "not listed above: `TreatmentRoomNotFound`."),
                 ("Which source answered is in the server log (and in `X-Emulator-*` headers with "
                  "`--source-headers`).")])]


def _catalog(app):
    catalog, maps = app.state.catalog, app.state.maps
    blocks = [_p(f"`{app.state.settings.data_dir}`: {catalog.summary()} · {maps.summary()}."),
              _h(2, "Rooms"),
              _table(["Name", "Machine name", "Room GUID"],
                     [[_text(room.name), f"`{json.dumps(room.machine_name)}`", f"`{room.id}`"]
                      for room in catalog.rooms]),
              _h(2, "Patients and surfaces")]
    for patient_id in sorted(catalog.known_patients):
        surfaces = catalog.surfaces_for(patient_id)
        blocks.append(_h(3, f"Patient `{patient_id}`"))
        blocks.append(_table(["Surface", "timeStamp"], [[f"`{s.id}`", _text(s.timestamp)] for s in surfaces])
                      if surfaces else _p("No surfaces (a success with an empty list)."))
    blocks += [_h(2, "GetMap and GetBeamDeliveryStatus"),
               _p("Computed by the emulator's clearance model (synthetic), unless one of the catalog's "
                  f"recorded maps matches ({maps.summary()}).")]
    return blocks


def _room_name(library, body):
    room = library.rooms.get(body.get("TreatmentRoomId"))
    return _text(room.get("name")) if room else "an unknown room"


def _setup(body):
    return (f"Couch buffer {body.get('CouchBuffer')} mm, patient buffer {body.get('PatientBuffer')} mm, "
            f"position {body.get('PatientPosition')}, isocenter {_isocenter(body)}")


def _isocenter(body):
    iso = body.get("Isocenter") or {}
    return f"({iso.get('x')}, {iso.get('y')}, {iso.get('z')})"


def _text(value):
    """A value for a table cell: as text, with the characters that would break the table escaped."""
    return "" if value is None else str(value).replace("|", "\\|").replace("`", "'")


# -- Markdown ----------------------------------------------------------------------------------

def markdown(blocks):
    out = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            out.append(f"{'#' * block[1]} {block[2]}")
        elif kind == "p":
            out.append(block[1])
        elif kind == "ul":
            out.append("\n".join(f"- {item}" for item in block[1]))
        elif kind == "table":
            headers, rows = block[1], block[2]
            out.append("\n".join([f"| {' | '.join(headers)} |", f"|{'---|' * len(headers)}",
                                  *(f"| {' | '.join(row)} |" for row in rows)]))
        elif kind == "code":
            out.append(f"```json\n{block[1]}\n```")
    return "\n\n".join(out) + "\n"


# -- HTML --------------------------------------------------------------------------------------

_INLINE = re.compile(r"`([^`]+)`|\*\*([^*]+)\*\*")

_STYLE = """
:root { --fg: #1d1d1f; --bg: #ffffff; --muted: #5f6368; --rule: #d9dce1; --code: #f3f4f6; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8eaed; --bg: #17181a; --muted: #a0a4ab; --rule: #3a3d42; --code: #24262a; }
}
body { font: 15px/1.5 system-ui, sans-serif; color: var(--fg); background: var(--bg);
       max-width: 72rem; margin: 2rem auto; padding: 0 1rem; }
h1, h2 { border-bottom: 1px solid var(--rule); padding-bottom: .2em; }
table { border-collapse: collapse; margin: .5rem 0 1rem; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--rule); padding: .3rem .6rem; text-align: left; vertical-align: top; }
code { background: var(--code); padding: 0 .25em; border-radius: 3px; font-size: 90%; }
pre { background: var(--code); padding: .75rem; border-radius: 6px; overflow-x: auto; }
pre code { padding: 0; background: none; }
"""


def _inline(text):
    escaped = html.escape(text.replace("\\|", "|"), quote=False)
    return _INLINE.sub(lambda m: f"<code>{m.group(1)}</code>" if m.group(1) is not None
                       else f"<strong>{m.group(2)}</strong>", escaped)


def html_page(blocks):
    body = []
    for block in blocks:
        kind = block[0]
        if kind == "h":
            body.append(f"<h{block[1]}>{_inline(block[2])}</h{block[1]}>")
        elif kind == "p":
            body.append(f"<p>{_inline(block[1])}</p>")
        elif kind == "ul":
            body.append("<ul>" + "".join(f"<li>{_inline(item)}</li>" for item in block[1]) + "</ul>")
        elif kind == "table":
            head = "".join(f"<th>{_inline(cell)}</th>" for cell in block[1])
            rows = "".join("<tr>" + "".join(f"<td>{_inline(cell)}</td>" for cell in row) + "</tr>"
                           for row in block[2])
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
        elif kind == "code":
            body.append(f"<pre><code>{html.escape(block[1], quote=False)}</code></pre>")
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{TITLE}</title><style>{_STYLE}</style></head>\n<body>\n"
            + "\n".join(body) + "\n</body></html>\n")
