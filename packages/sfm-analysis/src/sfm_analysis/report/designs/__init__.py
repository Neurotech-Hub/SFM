"""designs — the bundled report design JSON files as package data.

This module exists (rather than a bare directory) for two reasons:

  - setuptools' non-namespace package discovery only finds directories
    that are real packages, so without this file the *.json files below
    silently do not ship in the wheel while an editable install keeps
    working fine (see schema.py's DEFAULT_REPORTS_DIR).
  - importlib.resources.files() on a namespace package returns a
    MultiplexedPath, whose str() is not a usable filesystem path; a
    regular package avoids that entirely.
"""
