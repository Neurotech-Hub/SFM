"""Sanity check that the package is actually installed and importable, and
that its declared version and its __version__ attribute agree — a static
version string (see pyproject.toml's rationale for not using setuptools_scm)
is only safe if nothing lets the two drift apart silently."""

import importlib.metadata

import sfm_analysis


def test_importable():
    assert sfm_analysis.__version__


def test_version_matches_installed_metadata():
    assert importlib.metadata.version("sfm-analysis") == sfm_analysis.__version__
