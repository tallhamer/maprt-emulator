# MapRT Emulator

A local emulator of the Vision RT **MapRT API 2.0** REST endpoints, for teaching and for testing
MapRT clients without an appliance, and its **session packs**: anonymized recordings of a real
appliance's replies (treatment rooms, patient surfaces, collision maps and beam delivery checks,
exactly as the appliance sent them). With a pack loaded, the emulator answers like that appliance
did.

> **Not a medical device. Not for clinical use.** The emulator and the packs are for teaching and
> testing. A pack records what one appliance answered on one day; it says nothing about any
> patient's treatment. Not affiliated with or endorsed by Vision RT.

**The emulator's code arrives here with its first release** (a `v<x.y.z>` tag); until then this
repository holds the packs. Each pack is one patient (a phantom, or an anonymized patient): every
identifier is replaced, every date is shifted, and every file was checked for the originals before
it was written. [`docs/PACK_FORMAT.md`](docs/PACK_FORMAT.md) says what a pack holds, what its
anonymization guarantees, and how the emulator checks and serves it.

## Getting packs

The packs are this repository's **release assets**, not files in git: each release `packs-v1`,
`packs-v2`, … is one set, and each pack is its own download. The current set is
[**`packs-v1`**](https://github.com/tallhamer/maprt-emulator/releases/tag/packs-v1); its notes list
every pack (what it holds, its size, its sha256).

Download only the packs you need with [`get_packs.py`](get_packs.py) (Python 3.11+, nothing to
install). Nothing is downloaded unless you name packs or ask for `--all`, and each zip is checked
against its sha256:

```console
curl -LO https://raw.githubusercontent.com/tallhamer/maprt-emulator/packs-v1/get_packs.py
python get_packs.py --release packs-v1 --list
python get_packs.py --release packs-v1 hfdr-phantom srs-patient
python get_packs.py --release packs-v1 --all
```

The zips land in `packs/`, ready for `maprt-emulator --packs-dir packs`. Without Python, the
release's notes give one `curl -L` line per pack. No GitHub account is needed either way.

**Pin a release** in anything that must not change (a course, a notebook, a test): `--release
packs-v1`, or a `…/download/packs-v1/…` URL, never `…/latest/…`. A newer set can change or drop
packs, and an old set may be deleted once nothing uses it.

## How to cite

The packs: Tallhamer, M. J. (2026). *MapRT Emulator session packs* (packs-v1) [Data set].
AdventHealth. https://github.com/tallhamer/maprt-emulator

Michael J Tallhamer MSc DABR, AdventHealth. GitHub's "Cite this repository" button gives the
emulator's citation, with the packs' in its references ([`CITATION.cff`](CITATION.cff)).

## Licences

- **The packs:** [Creative Commons Attribution 4.0 International](LICENSE-packs) (CC BY 4.0): you
  may share and adapt them, for any purpose, including commercially, as long as you give
  appropriate credit (the citation above), link to the licence, and say if you changed them.
- **The code** (the emulator, `get_packs.py`): [MIT](LICENSE).

Both are provided as is, without warranty of any kind. MapRT is a product of Vision RT Ltd. Neither
the emulator nor the packs are made, endorsed or supported by Vision RT.
