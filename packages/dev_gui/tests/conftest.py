"""
Shared test setup.

Every test runs with a throwaway home directory. The base station keeps its
settings under ``~/.sfm`` (the MAC-to-ID registry, BNC settings, node layout,
the process lock) and its logs under ``~/sfm_logs``. Tests that assign IDs or
save settings would otherwise write fake boards into the real files of the Pi
they run on, and a fake MAC in the registry changes which ID a real node gets.
"""

from __future__ import annotations

import site
from pathlib import Path

import pytest

# Read before any test changes HOME, so a subprocess a test starts still finds
# the user's installed packages (python-can, the SDK).
_REAL_HOME = Path.home()
_USER_BASE = site.getuserbase()


@pytest.fixture(autouse=True)
def isolated_home(tmp_path_factory, monkeypatch):
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("PYTHONUSERBASE", _USER_BASE)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    return home

