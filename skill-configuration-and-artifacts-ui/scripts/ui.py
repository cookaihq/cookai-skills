from _runtime_bootstrap import ensure

ensure()

from pathlib import Path
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "assets" / "runtime"))
runpy.run_module("skill_ui", run_name="__main__")
