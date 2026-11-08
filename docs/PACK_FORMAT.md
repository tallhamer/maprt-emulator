# Session pack format, `maprt-pack/1`

This page is the contract between the tool that writes a pack (`maprt-pack`, the packing tool
behind the published packs) and the emulator that serves one. The format is fixed at
`maprt-pack/1` as of the first published set of packs, `packs-v1`: a change that an existing
reader couldn't follow would be a new format version, and the emulator would go on reading
`maprt-pack/1`.

A **session pack** is a `.zip` holding one patient's anonymized MapRT API sessions, recorded
against a real appliance by MapApp's Session Recording. It can also hold that patient's
anonymized DICOM plan, structure set and CT. The emulator loads packs and serves their data as
an appliance would.

## Layout

```
<pack>.zip
├── manifest.json
├── sessions/<session id>/      one folder per recorded session, in the Session Recording format
│   ├── session.json
│   ├── README.md                               a summary for people, rebuilt from this folder
│   ├── <endpoint>_<timestamp>_request.json
│   ├── <endpoint>_<timestamp>_response.json
│   └── <endpoint>_<timestamp>_response.txt     a non-JSON body (GetMap's map), byte for byte
└── dicom/                      optional, and each part optional on its own
    ├── plan/
    ├── struct/
    └── ct/
```

The session folders keep the recording format unchanged (`session.json` → `format`,
`"maprt-recording/1"`), so any tool that reads a recording reads a pack's sessions. There is
one addition: **`body_ref`** (below).

Each session's **`README.md`** lists its calls with what came back, and its successes and
failures, for a person to read. It is rebuilt from the anonymized files when the pack is written
(the recorded one is never copied), so it says nothing the records don't. A reader ignores it.

A session recorded by MapApp's **Session Capture** also has, in `session.json`:
- `capture`: what was asked for (the calls, the buffer ranges, the resolutions, the room and
  surface IDs, and whether a plan or the defaults set up the map and beam calls);
- `dicom`: `{"plan", "struct", "ct"}`, the names of that session's DICOM files **in the pack's
  `dicom/<part>/`** (empty where a part wasn't packed).

## `manifest.json`

| Field | Meaning |
|---|---|
| `format` | `"maprt-pack/1"` |
| `name`, `description` | What the pack is. The `name` is the zip's file name (`<name>.zip`) in a release; the description's first line is its title in the release's notes and its asset label |
| `created` | When the pack was written (ISO 8601, UTC) |
| `anonymizer` | The version of the tool that anonymized it |
| `anonymized` | `true`. A pack without it is refused. |
| `patient` | `{"id", "name"}`: the replacement values. Exactly one patient per pack. |
| `appliance` | `{"server", "apiVersion"}`: the recorded `Server` header and `ping` value. Never a hostname. |
| `counts` | `{"sessions", "calls", "surfaces", "maps", "beamChecks", "dicomFiles"}` |
| `sizes` | `{"uncompressed", "dicom"}` in bytes. The zip's own size is in the release's notes: a manifest can't know the size of the archive it's inside. |
| `includesDicom` | `{"plan", "struct", "ct"}`, each `true` or `false` |
| `files` | `{"<path in the zip>": "<sha256>"}` for every other file |
| `source` | *Optional.* What the pack was built from, as the person packing declared it (below). A pack without it is "source unknown". |

The manifest **never** holds the mapping from original values to replacements.

### `source`

```json
"source": {
  "kind": "patient",
  "reviewed": "2026-10-30",
  "labelsAccepted": {"RTPlanLabel": ["Plan01"], "ROIName": ["BODY", "PTV"]}
}
```

| Field | Meaning |
|---|---|
| `kind` | `"phantom"` or `"patient"` |
| `reviewed` | The date the pack was saved (ISO 8601, UTC). Not patient data, so not shifted. |
| `labelsAccepted` | `{"<DICOM keyword>": ["<value>", …]}`: the typed labels the anonymizer kept as written (plan label, name and ID, beam names, ROI names), exactly as the person packing saw them. They are already in the pack's DICOM. |

A `patient` pack is written only after the person packing has reviewed these labels and accepted
responsibility for publishing them. This records a decision; nothing in the pack can prove which
kind it is. The packer's name is never stored.

## `body_ref`: a repeated body stored once

A session often fetches the same surface more than once, and a surface is several megabytes. The
second and later copies of a response body of **1 KiB or more** that is byte-identical to one
already in the pack are stored as a reference rather than repeated. Sessions are taken in the order
they started and files by name, so "first" is well defined:

```json
{ "…": "…", "body_ref": "sessions/<session id>/<file>_response.json" }
```

in place of `body` (or `body_file`). A reader resolves the reference to that record's body, which
is never itself a `body_ref`: its `body`, or the `.txt` its `body_file` names (a map).

## What anonymization guarantees

Each pack is anonymized when it is written. Before writing, the tool checks every byte of every
file for the originals it knows, and every piece of free text for the original dates written in
any common form (for 3 February 2001: `20010203`, `020301`, `02/03/01`, `3 Feb 2001`, …). Then it records
`anonymized: true`. The rules:

- **Patient:** the patient ID, name and birth date are replaced with the values given when the
  pack is built. The same ID appears in the MapRT records and in the DICOM.
- **Surface IDs, session IDs, room IDs and names:** kept as recorded. A MapRT ID is unlikely to
  lead back to a patient, so anonymization keeps to names, IDs, birth dates, the site and the
  DICOM.
- **Dates:** every date moves back by one **random, secret offset per pack**, a whole number of
  weeks between 1 and 10 years (so the day of the week is kept). The offset is never written
  anywhere. The time of day and the value's formatting are unchanged (`started`, `sent`,
  `received`, `date` headers, surface `timeStamp`, file names, DICOM dates). Intervals and order
  survive.
- **Labels:** the structure set's label and name, and the descriptive labels nothing reads (dose
  reference, fraction group, patient setup, ROI observation and ROI generation), become their
  element's own name (`StructureSetLabel` → `StructureSet`). The plan label, name and ID, beam
  names and ROI names are kept as written and listed for review; one that looks like a date is
  flagged, to be changed in the TPS. A kept label holding one of the patient's own dates is
  listed the same way, and kept if the person packing accepts the labels. The pack's description
  is handled like a label: a date-like one is flagged, and one holding the patient's own date is
  listed and kept if the person packing goes ahead. An original date anywhere else in free text
  (other DICOM text, error texts, OBJ comments) refuses the pack.
- **Bodies** change only where one of these rules names a key in them, so a pack replays what
  crossed the wire.
- **Error-only sessions** are not packed: every session has a successful data call.
- **Sessions:** `session.json` carries `"format": "maprt-recording/1"`.
- **Site:** every `url` keeps only its path, `api_url` is `null`, httpx's `host` header is dropped,
  and the hostname in a transport `error` text becomes `<appliance>`.
- **DICOM:**
  - every UID is remapped consistently across the plan, structure set and CT;
  - file names are rewritten to match the new UIDs;
  - institution, station, operator and device identifiers are removed;
  - private tags are removed;
  - `TreatmentMachineName` becomes `TreatmentMachine`;
  - `PatientIdentityRemoved` is set to `YES`.
- **Geometry** (vertices, maps, isocenters, control points) is unchanged.

## When the emulator loads a pack

The originals are gone by then, so the emulator checks what a pack must look like. It checks every
pack it's given, collects **every** finding, and reports them together, so one run shows everything
to fix.

**The checks** (the same as `maprt-pack check`, plus the emulator's own):

- `manifest.json` exists, its `format` is `maprt-pack/1`, and `anonymized` is `true`;
- `source`, when present, has a `kind` of `phantom` or `patient`, a `reviewed` date, and
  `labelsAccepted` as keywords to lists of text (a pack without it loads, as "source unknown", and
  the inventory page says so);
- every file is listed in `files` with a matching `sha256`, and every listed file exists (a pack
  folder's hidden files, such as a Mac's `.DS_Store`, are ignored);
- every `url` is a path with no scheme or host, `api_url` is `null`, and no record has a `host`
  header;
- every `/patients/{id}` URL names the manifest's patient;
- every `body_ref` resolves to a body that isn't itself a reference;
- every DICOM file declares `PatientIdentityRemoved = YES` and the manifest's patient ID;
- each `session.json` is `maprt-recording/1` (or has no `format`), each record is named as the
  recorder names them, and no response is without its request;
- `appliance.server` reads as `Saturn/<numbers>` and `apiVersion` is a string.

A request with no response, or a response that records only a transport error, is a call that
never got a reply: it's counted and never served.

**Which packs are served.** Packs are chosen with `--packs-dir` (every `.zip` and every folder with a
`manifest.json` directly inside it) and, optionally, `--pack NAME` (repeatable). A pack's name is
its manifest's `name`, not its file name.

- **A named pack** (`--pack`) that fails a check **refuses startup**, as does a name that matches no
  pack. Nothing is served.
- **A scanned pack** (found in `--packs-dir`, with no `--pack`) that fails a check is **skipped
  entirely**, with its findings in the log, the start banner and `server.json`; the others are
  served. If **every** scanned pack fails, startup is refused. An empty folder loads no packs.
- **Packs that disagree refuse startup**, named or scanned: two packs with one name, one room GUID
  with different fields, one surface with different bytes, one listed surface with different
  fields, or the newest appliance with two API versions. Neither pack is wrong on its own; the
  error names both, and `--pack` chooses between them.
- Every refusal says how to start without packs: `--no-packs` (or `MAPRT_EMULATOR_NO_PACKS=1`)
  serves the built-in synthetic catalog and reads no pack.

**What's served from them:**

- **Rooms** are the union of every pack's recorded room list, by GUID, in first-seen order.
  `/rooms/{machineName}` is answered from that union, never from a recorded lookup.
- **A patient's surfaces** are the union of their recorded lists, by surface ID.
- **A surface listed but never fetched** (in any loaded pack) stays in the list. Fetching it answers
  `SurfaceNotFound` (HTTP 200, `PatientErrorCodes`, as a real appliance answers an unknown surface),
  and so does a GetMap or GetBeamDeliveryStatus naming it that wasn't recorded. A real appliance
  would send the mesh; with none recorded, an error is the only honest answer. Each one is
  reported at startup.
- **Maps and beam checks** are matched on the request: the fields the emulator's request models
  know, as it parses them, so `20` matches `20.0`, `PatientPosition` `1` matches `"HFS"`, `-0.0`
  matches `0.0`, a field the appliance ignores doesn't count, and key order doesn't matter. Values
  are otherwise matched exactly. When one request was recorded more than once, the first reply is
  served. **A GetMap that wasn't recorded** gets a map recorded for another request, of the requested
  resolution (real appliance output, but not this request's data; the same request always gets the
  same one), or, with none of that resolution, a computed (synthetic) map. The server log says
  which. A surface listed but never fetched gets neither (above). **A beam check that wasn't
  recorded** is derived from the high-resolution map recorded for the same surface, room, position,
  buffers and isocenter (a beam is safe when every control point, to the whole degree, is clear on
  it), when every control point is on that map; otherwise it's computed (synthetic).
- **Recorded errors** are served for the request they answered, unless a success was recorded for
  the same thing anywhere. Errors that depend on the caller (`AuthErrorCodes`, and any non-200
  reply) aren't served from packs: the emulator checks the token itself.
- **Packs from different appliance versions:** the emulator presents one appliance, the **newest**
  (`Saturn/` versions compared as numbers), with that pack's API version, in the `Server` header and
  the ping reply. Every other body is served as recorded. The banner and the log name the older
  versions and their packs.
