import os
from pathlib import Path
import sys

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

# Require a working, correctly versioned Pandoc: no silently skipped core tests.
from pdf2markdown import assets

_find_pandoc = assets.find_pandoc
PANDOC = _find_pandoc(os.environ.get("PANDOC_TEST_BIN"))
assets.find_pandoc = lambda explicit_path=None: _find_pandoc(explicit_path or PANDOC)


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    import socket

    def refused(*args, **kwargs):
        raise AssertionError("Tests must inject transports; live network is forbidden")

    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
