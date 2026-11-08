#!/usr/bin/env python3
from __future__ import annotations

import os
import sys


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

from video_to_markdown.cli import main


if __name__ == "__main__":
    # JSON and recovery paths use UTF-8 even when Windows redirects a legacy-codepage console.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
