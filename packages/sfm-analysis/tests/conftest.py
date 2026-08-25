"""Shared setup for the sfm-analysis test suite.

Deliberately does NOT touch sys.path. The SDK is expected to be installed
(``pip install -e ".[dev]"``), and pytest's default prepend import mode
already puts this directory on sys.path — which is what makes the plain
``from report_fixtures import ...`` in several test modules work.

Requiring a real install is the point: it is what proves the package is
importable from a clean environment, not just from a repo checkout.
"""
