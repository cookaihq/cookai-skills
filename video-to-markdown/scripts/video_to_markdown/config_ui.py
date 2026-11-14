from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path


def run(args):
    from skill_ui import __main__ as service
    project = Path(args.project).expanduser().resolve()
    # Distinct startup options cannot silently reuse a service with stale routing evidence.
    caps = Path(args.capabilities).resolve() if args.capabilities else None
    fingerprint = hashlib.sha256((args.host + str(args.use_local_key) + (caps.read_text(encoding="utf-8") if caps else "")).encode()).hexdigest()[:12]
    runtime = (Path(args.runtime_dir) if args.runtime_dir else Path.home() / ".cache" / "video-to-markdown" / "ui") / fingerprint
    os.environ["VIDEO_VISION_UI_HOST"] = args.host
    os.environ["VIDEO_VISION_UI_HOME"] = "1" if args.use_local_key else "0"
    if caps:
        os.environ["VIDEO_VISION_UI_CAPABILITIES"] = str(caps)
    else:
        os.environ.pop("VIDEO_VISION_UI_CAPABILITIES", None)
    sys.argv = ["skill_ui", args.action, "--project", str(project), "--page", "configuration",
                "--adapter", str(Path(__file__).resolve().parents[1] / "config_ui_adapter.py"),
                "--runtime-dir", str(runtime), "--idle-seconds", "3600"]
    service.main()
    return 0
