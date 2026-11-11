"""AS public entry point, including direct-Python runtime repair."""
from _runtime_bootstrap import ensure

ensure()

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "shared"), str(ROOT)]
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
from as_runtime.cli import main

if __name__ == "__main__":
    main()
