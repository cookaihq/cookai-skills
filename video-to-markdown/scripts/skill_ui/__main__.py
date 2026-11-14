import argparse
import hashlib
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, Request, build_opener

from .contracts import UIError
from .server import Server
from .storage import atomic_json, file_lock, private_directory


def request(origin, path, data=None):
    raw = json.dumps(data).encode() if data is not None else None
    req = Request(origin + path, data=raw, headers={"Origin": origin, "Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(req, timeout=2) as response:
        return json.load(response)


def running(directory):
    try:
        record = json.loads((directory / "server.json").read_text(encoding="utf-8"))
        state = request(record["origin"], "/health")
        if state.get("app") == "skill-ui" and state.get("instance") == record["instance"]:
            return {**record, "lifetime": state["lifetime"]}
    except (OSError, ValueError, KeyError):
        pass
    return None


def load_adapter(path, project):
    spec = importlib.util.spec_from_file_location("business_ui_adapter", path)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    return module.build(project)


def wait_for_exit(directory, timeout=5):
    """An unhealthy listener may still be closing; wait for its process-owned lock."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            with file_lock(directory / "service.lock"):
                return
        except UIError as exc:
            if exc.code != "busy":
                raise
            if time.monotonic() >= deadline:
                raise UIError("stopping", "服务尚未完成退出，请稍后查询状态。") from None
            time.sleep(0.05)


def remove_record(directory, instance):
    # Windows readers briefly deny file deletion. Retry without releasing service ownership.
    path = directory / "server.json"
    deadline = time.monotonic() + 5
    while True:
        try:
            if not path.exists():
                return
            if json.loads(path.read_text(encoding="utf-8")).get("instance") == instance:
                path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def serve(args, directory):
    with file_lock(directory / "service.lock"):
        handler = RotatingFileHandler(directory / "service.log", maxBytes=1024*1024, backupCount=2, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
        adapter = load_adapter(args.adapter, args.project)
        server = Server(adapter, args.project, args.page, directory, args.idle_seconds, args.public_origin)
        record = {"pid": os.getpid(), "origin": server.origin, "public_origin": server.public_origin,
                  "instance": server.instance, "token": server.token, "page": args.page,
                  "project": str(args.project), "adapter": str(args.adapter)}
        atomic_json(directory / "server.json", record, owner_path=args.project)
        logging.info("service_started page=%s instance=%s", args.page, server.instance)
        threading.Thread(target=server.watcher, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            server.closed.set()
            server.server_close()
            remove_record(directory, server.instance)
            logging.info("service_stopped page=%s instance=%s", args.page, server.instance)


def main():
    parser = argparse.ArgumentParser(description="Owned local UI service; adapter is trusted business code.")
    parser.add_argument("command", choices=["start", "status", "stop", "serve"])
    parser.add_argument("--adapter", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--page", choices=["configuration", "artifacts"], required=True)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Ignored private directory outside the install")
    parser.add_argument("--idle-seconds", type=int, default=3600)
    parser.add_argument("--public-origin", help="Exact verified proxy origin; networking is configured separately")
    args = parser.parse_args()
    args.adapter, args.project = args.adapter.resolve(), args.project.resolve()
    if not args.adapter.is_file() or not args.project.is_dir():
        raise UIError("path", "适配器文件或项目目录不存在。")
    key = hashlib.sha256((str(args.adapter) + "\0" + str(args.project) + "\0" + args.page).encode()).hexdigest()[:20]
    directory = args.runtime_dir.resolve() / key
    if args.command in ("status", "stop") and not directory.exists():
        print(json.dumps({"status": "stopped", "runtime": str(directory)}))
        return
    private_directory(directory, owner_path=args.project)
    if args.command == "serve":
        return serve(args, directory)
    with file_lock(directory / "launcher.lock"):
        record = running(directory)
        if args.command == "start" and record:
            if args.public_origin and args.public_origin != record["public_origin"]:
                raise UIError("origin", "已有服务使用不同访问地址，请先停止该页面再重开。")
        if args.command == "start" and not record:
            wait_for_exit(directory)
            child_args = [sys.executable, "-m", "skill_ui", "serve", "--adapter", str(args.adapter), "--project", str(args.project),
                          "--page", args.page, "--runtime-dir", str(args.runtime_dir.resolve()), "--idle-seconds", str(args.idle_seconds)]
            if args.public_origin:
                child_args.extend(["--public-origin", args.public_origin])
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent.parent))
            # Startup failures report safe type only, no imported adapter exception text.
            options = {"stdin": subprocess.DEVNULL, "cwd": str(args.project), "env": environment}
            if os.name == "nt":
                options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                options["start_new_session"] = True
            with (directory / "launcher.log").open("ab") as stream:
                process = subprocess.Popen(child_args, stdout=stream, stderr=stream, **options)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                record = running(directory)
                if record:
                    break
                if process.poll() is not None:
                    raise UIError("startup", "服务启动失败，请检查 " + str(directory / "launcher.log"))
                time.sleep(0.1)
            if not record:
                raise UIError("startup", "启动未完成，请先查询状态；不要直接重复创建进程。")
        if args.command == "stop":
            if record:
                request(record["origin"], "/api/control/stop", {"token": record["token"]})
            wait_for_exit(directory)
            record = None
        elif args.command == "status" and not record:
            wait_for_exit(directory)
        output = {"status": "running" if record else "stopped", "runtime": str(directory)}
        if record:
            output.update(instance=record["instance"], page=record["page"], project=record["project"], lifetime=record["lifetime"])
            if args.command == "start":
                output["url"] = record["public_origin"] + "/#" + record["token"]
        saved = directory / "saved.json"
        if record and saved.exists():
            receipt = json.loads(saved.read_text(encoding="utf-8"))
            if receipt.get("instance") == record["instance"]:
                output["saved"] = receipt
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except UIError as exc:
        print(json.dumps({"error": exc.code, "message": exc.message}, ensure_ascii=False))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"error": "internal", "type": type(exc).__name__, "message": "启动或状态操作失败；核对路径、权限与适配器。"}, ensure_ascii=False))
        sys.exit(1)
