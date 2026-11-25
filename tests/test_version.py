"""``maprt_emulator.__version__`` names the installed package's version (0.1.1 shipped saying 0.1.0)."""

from importlib.metadata import version

import maprt_emulator


def test_dunder_version_matches_the_package_metadata():
    assert maprt_emulator.__version__ == version("maprt-emulator")
