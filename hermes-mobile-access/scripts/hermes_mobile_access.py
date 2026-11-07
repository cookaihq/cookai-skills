#!/usr/bin/env python3
"""Single locked entry for discovery, saved connections and the local UI."""
import sys
from bootstrap import ensure_runtime

if __name__ == "__main__":
    try:
        ensure_runtime()
    except RuntimeError as exc:
        import json
        print(json.dumps({"result": "runtime_unavailable", "detail": str(exc)}, ensure_ascii=False))
        sys.exit(2)

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path

from configuration import ConfigError, ConfigStore, draft, new_id, missing_binding
from frp_client import FrpClient
from domain_probe import probe_domain, server_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from skill_ui.contracts import UIError
from skill_ui.storage import atomic_json, file_lock, private_directory


def parser():
    p = argparse.ArgumentParser(description="Hermes 手机接入；当前开发入口尚未通过真实连接验收")
    p.add_argument("--project", type=Path, default=Path.cwd())
    p.add_argument("--home", type=Path, default=Path.home() / ".config/hermes-mobile-access")
    p.add_argument("--use-local-key", action="store_true", help="显式启用个人连接配置")
    p.add_argument("--scope", choices=("personal", "project"), default="personal")
    p.add_argument("--connection", help="本次稳定连接 ID；不修改默认项")
    p.add_argument("--frp-skill", type=Path, default=ROOT.parent / "frpc-launch")
    p.add_argument("--frp-project", type=Path)
    p.add_argument("--frp-home", type=Path, default=Path.home() / ".config/frpc-launch")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("discover", help="只读发现当前 Desktop 后端")
    d.add_argument("--ledger", type=Path)
    d.add_argument("--selected", help="明确选择的发现候选 ID")
    roster = sub.add_parser("profiles", help="验证当前 Desktop Gateway，读取真实 profile；不修改 Hermes")
    roster.add_argument("--selected", help="明确选择的发现候选 ID")
    access = sub.add_parser("probe-access", help="临时本机认证/WS 探针；不启动 FRP 或发送聊天")
    access.add_argument("--selected", help="明确选择的发现候选 ID")
    domain = sub.add_parser("probe-domain", help="访问候选域名；正常响应为占用，否则未占用，不启动 FRP")
    domain.add_argument("--domain-prefix", help="本次待检查的服务子域名；默认使用已保存值或 hermes-mobile")
    for command in ("plan", "connect", "status", "doctor", "disconnect", "context-info"):
        item = sub.add_parser(command)
        item.add_argument("--selected", help="明确选择的本次 Desktop 候选 ID")
    worker = sub.add_parser("_serve", help=argparse.SUPPRESS)
    worker.add_argument("directory", type=Path)
    worker.add_argument("generation")
    c = sub.add_parser("connections", help="命名连接配置；不启动业务连接")
    c.add_argument("operation", choices=("list", "new", "rename", "delete", "default"))
    c.add_argument("--name")
    u = sub.add_parser("configure", help="打开或管理本机配置页面")
    u.add_argument("operation", choices=("start", "status", "stop"), default="start", nargs="?")
    u.add_argument("--runtime-dir", type=Path)
    u.add_argument("--selected", help="本次页面明确选择的 Desktop 候选 ID；不持久绑定 PID")
    return p


def frp(args, cid=None, record=None):
    binding = (record or {}).get("frp", {})
    return FrpClient(args.frp_skill, binding.get("project") or args.frp_project or args.project,
                     binding.get("home") or args.frp_home, cid)


def summary(snap):
    return {"source": snap.source, "selector_source": snap.selector_source,
            "default_connection": snap.selector,
            "connections": [{"id": cid, "name": record["name"], "missing": missing_binding(record)}
                            for cid, record in snap.collection["connections"].items()]}


def configure(args):
    options = {"project": str(args.project.resolve()), "home": str(args.home.resolve()),
               "scope": args.scope, "use_local_key": args.use_local_key, "connection": args.connection,
               "frp_skill": str(args.frp_skill.resolve()), "frp_home": str(args.frp_home.resolve()),
               "frp_project": str((args.frp_project or args.project).resolve()), "candidate": args.selected}
    key = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:24]
    runtime = (args.runtime_dir or args.home / "runtime/ui") / key
    if args.operation == "start":
        private_directory(runtime, owner_path=args.project)
        with file_lock(runtime / "context.lock"):
            atomic_json(runtime / "launch.json", options, owner_path=args.project)
    os.environ["HMA_UI_CONTEXT"] = str((runtime / "launch.json").resolve())
    from skill_ui.__main__ import main as ui_main
    previous = sys.argv
    try:
        sys.argv = [previous[0], args.operation, "--adapter", str(ROOT / "ui/adapter.py"),
                    "--project", str(args.project), "--page", "configuration", "--runtime-dir", str(runtime)]
        ui_main()
    finally:
        sys.argv = previous


def execute(args):
    if args.command == "_serve":
        from connection_runtime import serve
        serve(args.directory, args.generation)
        return None
    if args.command == "discover":
        from discovery import discover_live
        return discover_live(ledger=args.ledger, selected=args.selected)
    if args.command == "profiles":
        from desktop_profiles import read_profiles
        return read_profiles(selected=args.selected)
    if args.command == "probe-access":
        from access_probe import probe_access
        return probe_access({"project": str(args.project.resolve()), "home": str(args.home.resolve()),
            "use_local_key": args.use_local_key, "connection": args.connection, "selected": args.selected,
            "frp_skill": str(args.frp_skill.resolve()), "frp_home": str(args.frp_home.resolve())})
    if args.command == "configure":
        configure(args)
        return None
    if args.command == "context-info":
        return frp(args).context()
    store = ConfigStore(args.project, home=args.home, use_local_key=args.use_local_key, scope=args.scope)
    from connection_runtime import Manager
    manager = Manager(store, args.frp_skill, getattr(args, "selected", None))
    if args.command in ("status", "disconnect") and args.connection:
        return manager.status(args.connection) if args.command == "status" else manager.disconnect(args.connection)
    snap = store.snapshot()
    if args.command == "probe-domain":
        if not snap.collection["connections"] and not args.connection:
            cid, record = None, draft()
        else:
            cid, record = snap.select(args.connection)
        context = frp(args, cid, record).context()
        suffix = context["domain_suffix"]
        if suffix.get("status") != "configured":
            raise ConfigError("frp_domain_suffix", "请先修复 FRP Launch 的父域来源。")
        url = server_url(args.domain_prefix if args.domain_prefix is not None else record["domain_prefix"], suffix["value"])
        return {"connection": cid, **probe_domain(url), "publication": "not_performed"}
    if args.command == "connections":
        if args.operation == "list":
            return summary(snap)
        collection = copy.deepcopy(snap.collection)
        default = None
        active = []
        if args.operation == "new":
            cid = new_id()
            collection["connections"][cid] = draft(args.name or "我的 Hermes")
            if not snap.selector and not snap.collection["connections"]:
                default = cid
        else:
            cid, record = snap.select(args.connection)
            if args.operation == "rename":
                if args.name is None:
                    raise ConfigError("name_required", "请提供新的显示名称。")
                collection["connections"][cid]["name"] = args.name
            elif args.operation == "default":
                default = cid
            elif args.operation == "delete":
                if manager.status(cid).get("controller_running"):
                    raise ConfigError("active_connection", "请先断开受管连接再删除。")
                state = frp(args, cid, record).status()
                if state.get("modes", {}).get("official", {}).get("running") is not False:
                    raise ConfigError("active_connection", "请先断开并核对本连接资源停止，再删除。")
                del collection["connections"][cid]
        return {"result": "saved", "connection": cid,
                **summary(store.save(snap.revision, collection, default_id=default, active_ids=active))}
    cid, record = snap.select(args.connection)
    client = frp(args, cid, record)
    if args.command == "disconnect":
        return manager.disconnect(cid)
    if args.command == "status":
        return manager.status(cid)
    if args.command == "connect":
        return manager.connect(cid)
    if args.command == "doctor":
        return {"connection": cid, "runtime": manager.status(cid), "frp": client.status(),
                "context": client.context(), "missing": missing_binding(record)}
    context = client.context()
    prefix, suffix = record["domain_prefix"], context["domain_suffix"].get("value", "")
    blocks = missing_binding(record)
    if context.get("connection", {}).get("status") != "configured":
        blocks.append("frp_connection")
    if sys.platform not in ("win32", "darwin"):
        blocks.append("supported_desktop_os_required")
    url, occupancy = None, None
    if context["domain_suffix"].get("status") != "configured" or not suffix or len(prefix + "." + suffix) > 253:
        blocks.append("frp_domain_suffix")
    else:
        url = server_url(prefix, suffix)
        occupancy = probe_domain(url)
        if occupancy["occupancy"] == "occupied":
            blocks.append("domain_occupied")
    return {"result": "plan_incomplete" if blocks else "planned",
            "connection": cid, "name": record["name"], "source": snap.source,
            "server_url": url, "domain_check": occupancy,
            "preferred_profile": record["hermes"].get("preferred_profile"),
            "missing": blocks, "publication": "not_performed",
            "detail": "连接时先验证公网认证，再启用后端并核验 REST/WS；手机聊天由用户确认。"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        result = execute(parser().parse_args())
        if result is not None:
            print(json.dumps(result, ensure_ascii=False))
        return 4 if result and result.get("result") in ("security_blocked", "plan_incomplete", "profiles_unavailable", "access_probe_unavailable", "failed", "cleanup_failed") else 0
    except ConfigError as exc:
        print(json.dumps(exc.report(), ensure_ascii=False))
        return 4
    except UIError as exc:
        print(json.dumps({"result": exc.code, "detail": exc.message}, ensure_ascii=False))
        return 4
    except (OSError, ValueError, KeyError) as exc:
        print(json.dumps({"result": "local_error", "type": type(exc).__name__,
                          "detail": "本机配置或入口异常，请检查来源与文件权限。"}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
