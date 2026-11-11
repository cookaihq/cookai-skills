import argparse
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

from skill_ui.contracts import UIError
from skill_ui.storage import prepare_private_write, private_directory
from .catalog import discover
from .config import Store, default_path, resolve
from .launch import Launcher

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description="AS Agent 会话启动器")
    parser.add_argument("--config-file", type=Path, default=default_path(), help="Explicit isolated configuration target")
    parser.add_argument("--runtime-dir", type=Path, help="Private runtime directory; default beside configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    commands.add_parser("config")
    for name in ("resolve", "launch"):
        sub = commands.add_parser(name)
        sub.add_argument("--current-agent", required=True, choices=["codex", "claude", "workbuddy"])
        sub.add_argument("--input-file", type=Path, required=True, help="UTF-8 exact original invocation")
        if name == "launch":
            sub.add_argument("--request-id", required=True)
    for name in ("inspect", "resume"):
        sub = commands.add_parser(name)
        sub.add_argument("--request-id", required=True)
    ui = commands.add_parser("ui")
    ui.add_argument("action", choices=["start", "status", "stop"])
    ui.add_argument("--idle-seconds", type=int, default=3600)
    ui.add_argument("--public-origin")
    args = parser.parse_args()
    cwd = Path.cwd().resolve()
    store = Store(args.config_file)
    runtime = (args.runtime_dir or store.path.parent / "runtime").resolve()
    try:
        if args.command == "ui":
            from skill_ui.__main__ import main as ui_main
            os.environ["AS_UI_CONFIG_FILE"] = str(store.path)
            config_key = hashlib.sha256(str(store.path).encode()).hexdigest()[:16]
            sys.argv = [sys.argv[0], args.action, "--adapter", str(ROOT / "ui/adapter.py"),
                        "--project", str(cwd), "--page", "configuration", "--runtime-dir", str(runtime / "ui" / config_key),
                        "--idle-seconds", str(args.idle_seconds)]
            if args.public_origin:
                sys.argv += ["--public-origin", args.public_origin]
            ui_main()
            return
        if args.command == "catalog":
            output = {"catalog": discover(cwd)}
        elif args.command == "config":
            settings, revision = store.read()
            output = {"settings": settings, "revision": revision, "source": str(store.path)}
        elif args.command == "resolve":
            settings, revision = store.read()
            output = resolve(args.input_file.read_text(encoding="utf-8-sig", newline=""), args.current_agent, settings)
        else:
            launcher = Launcher(runtime / "launches")
            if args.command == "inspect":
                output = launcher.inspect(args.request_id)
            else:
                prepare_private_write(runtime / "as.log", private_directory=runtime, owner_path=cwd)
                private_directory(runtime, owner_path=cwd)
                handler = RotatingFileHandler(runtime / "as.log", maxBytes=1024*1024, backupCount=2, encoding="utf-8")
                logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
                if args.command == "resume":
                    output = launcher.resume(args.request_id)
                else:
                    with args.input_file.open(encoding="utf-8-sig", newline="") as stream:
                        invocation = stream.read()
                    # Existing requests bypass discovery and configuration changes entirely.
                    if launcher.path(args.request_id).exists():
                        catalog = {}
                    else:
                        catalog = discover(cwd)
                    output = launcher.launch(args.request_id, invocation, args.current_agent, cwd, store, catalog)
        print(json.dumps(output, ensure_ascii=False))
        if isinstance(output, dict) and output.get("status") in ("unknown", "unconfirmed", "inflight"):
            raise SystemExit(2)
    except (UIError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, UIError) else "local_error"
        message = str(exc) if isinstance(exc, UIError) else "本地文件或数据不可用，请核对输入路径、权限与文件格式。"
        print(json.dumps({"error": code, "message": message}, ensure_ascii=False))
        raise SystemExit(1)
