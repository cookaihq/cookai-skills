"""Public consumer contexts: source selection and runtime ownership are separate.

Only non-secret routes are persisted here. Connection secrets remain owned by
project_config and reach the existing runner through its private environment.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path

import project_config as projects

INTERFACE_VERSION = 1
GLOBAL_FIELDS = ("transport", "user", "metadatas", "dnsServer", "loginFailExit")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_identity(consumer, instance):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", consumer or ""):
        raise projects.ProjectError("consumer 必须是小写字母开头的稳定标识。")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}", instance or ""):
        raise projects.ProjectError("instance 必须是 1–96 位字母、数字、下划线或连字符。")


def runtime_home(args):
    validate_identity(args.consumer, args.instance)
    return args.home / "consumers" / args.consumer / args.instance


def identity(args):
    validate_identity(args.consumer, args.instance)
    return "consumer-" + digest([os.path.normcase(str(args.home.resolve())),
                                projects.project_id(args.project), args.consumer,
                                args.instance])[:32]


def _context(environ, project, home):
    result = {"interface_version": INTERFACE_VERSION, "capabilities": ["owner_watch", "require_tls"], "project": str(project),
              "home": str(home), "public_endpoint": "not_checked"}
    suffix, source = "", ""
    try:
        suffix, source = projects.scalar(projects.SUFFIX_KEY, environ, project, home)
        suffix = projects.validate_domain(suffix) if suffix else ""
        result["domain_suffix"] = {"value": suffix, "source": source,
                                   "status": "configured" if suffix else "missing",
                                   "basis": "domain_suffix_setting" if suffix else "",
                                   "verification": "configuration_only"}
    except projects.ProjectError as exc:
        exc.source = exc.source or source
        result["domain_suffix"] = {"value": "", "source": exc.source, "status": "invalid", "error": exc.report()}
    connection, document = None, None
    try:
        native, source = projects.scalar("FRPC_LAUNCH_CONFIG", environ, project, home)
        if native:
            raise projects.ProjectError("原生完整配置入口需要先明确迁移。",
                                        code="native_config_present", source=source)
        connection = projects.resolve_connection(environ, project, home)
        result["connection"] = {"status": "configured", **connection.report()}
    except projects.ProjectError as exc:
        result["connection"] = {"status": "unavailable", "error": exc.report()}
    # Consumers derive a service hostname beneath the effective FRP connection
    # host when no separate naming suffix was configured. This is a naming
    # convention, not evidence of DNS, TLS or frps subDomainHost configuration.
    # Reuse the whole selected connection; never fill from a lower layer.
    if connection and result["domain_suffix"]["status"] == "missing":
        result["domain_suffix"].update(source=connection.source, basis="server_addr")
        try:
            ipaddress.ip_address(connection.addr.strip("[]"))
        except ValueError:
            try:
                suffix = projects.validate_domain(connection.addr)
                result["domain_suffix"].update(value=suffix, status="configured")
            except projects.ProjectError:
                result["domain_suffix"]["reason"] = "server_addr_not_domain"
        else:
            result["domain_suffix"]["reason"] = "server_addr_is_ip"
    try:
        document = projects.load_project(project)
        result["saved_project_domain"] = document.get("domain", "")
        result["project_configuration"] = {"status": "readable"}
    except projects.ProjectError as exc:
        result["saved_project_domain"] = ""
        result["project_configuration"] = {"status": "invalid", "error": exc.report()}
    # Opaque revision invalidates observations on source, credential or routing
    # changes; it is not a claim about the identity of a remote FRPS account.
    result["revision"] = digest([result, vars(connection) if connection else None, document])
    result["source_binding"] = digest([str(project), str(home),
        [connection.source, connection.addr, connection.port] if connection else None])
    return result, connection, document


def context_report(environ, project, home):
    return _context(environ, project, home)[0]


def require_context(environ, args):
    report, connection, document = _context(environ, args.project, args.home)
    if not connection or document is None or not report["domain_suffix"]["value"]:
        raise projects.ProjectError("FRP 上下文不完整；用 context-info 查看来源与缺项。",
                                    code="context_unavailable", exit_code=4)
    return report, connection, document


def route_path(args):
    return runtime_home(args) / "route.json"


def load_route(args):
    path = route_path(args)
    try:
        route = json.loads(projects.read_text(path))
    except (ValueError, TypeError):
        raise projects.ProjectError("独立连接路由缺失或损坏；先运行 instance-config。",
                                    source=str(path), code="invalid_route", exit_code=4) from None
    if (not isinstance(route, dict) or route.get("schema_version") != 1
            or route.get("owner") != identity(args)):
        raise projects.ProjectError("独立连接来源或所有权不匹配。", code="ownership_mismatch")
    if route.get("consumer") != args.consumer or route.get("instance") != args.instance:
        raise projects.ProjectError("独立连接标识不匹配。", code="ownership_mismatch")
    proxy = route.get("proxy")
    if (not isinstance(proxy, dict)
            or set(proxy) - {"name", "type", "localIP", "localPort", "customDomains", "locations"}
            or proxy.get("name") != identity(args)
            or proxy.get("type") not in ("http", "https")
            or proxy.get("localIP") not in ("127.0.0.1", "::1")
            or type(proxy.get("localPort")) is not int or not 1 <= proxy["localPort"] <= 65535
            or proxy.get("customDomains") != [route.get("domain")]):
        raise projects.ProjectError("独立路由字段无效；未使用或回显记录。", code="invalid_route")
    domain = projects.validate_domain(route.get("domain"))
    prefix = route.get("domain_prefix")
    locations = proxy.get("locations", [])
    if (not isinstance(prefix, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", prefix)
            or not domain.startswith(prefix + ".") or not isinstance(locations, list)
            or any(not isinstance(p, str) or not p.startswith("/") for p in locations)
            or (locations and proxy["type"] != "http")):
        raise projects.ProjectError("独立路由域名或路径无效。", code="invalid_route")
    return route


def make_route(args, report):
    prefix = args.domain_prefix.lower()
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", prefix):
        raise projects.ProjectError("domain-prefix 只接受单个 DNS 标签。")
    domain = projects.validate_domain(prefix + "." + report["domain_suffix"]["value"])
    if args.local_ip not in ("127.0.0.1", "::1"):
        raise projects.ProjectError("独立连接仅允许明确的 loopback 本地目标。")
    if not 1 <= args.local_port <= 65535:
        raise projects.ProjectError("local-port 必须为 1–65535。")
    locations = args.locations or []
    if locations and (args.proxy_type != "http" or any(not p.startswith("/") for p in locations)):
        raise projects.ProjectError("locations 仅供 HTTP 代理使用，且必须以 / 开头。")
    proxy = {"name": identity(args), "type": args.proxy_type, "localIP": args.local_ip,
             "localPort": args.local_port, "customDomains": [domain]}
    if locations:
        proxy["locations"] = locations
    return {"schema_version": 1, "owner": identity(args), "consumer": args.consumer,
            "instance": args.instance, "project": str(args.project), "home": str(args.home),
            "context_revision": report["revision"], "source_binding": report["source_binding"],
            "domain_prefix": prefix, "domain": domain, "proxy": proxy,
            "require_tls": bool(getattr(args, "require_tls", False))}


def _globals(client, base):
    # Drop tomlkit's dotted-table presentation metadata when composing a new
    # document; retaining it can silently omit tables during serialization.
    client = client.unwrap() if hasattr(client, "unwrap") else client
    result = {k: copy.deepcopy(v) for k, v in client.items() if k in GLOBAL_FIELDS}
    tls = result.get("transport", {}).get("tls", {})
    for key in ("certFile", "keyFile", "trustedCaFile"):
        value = tls.get(key)
        if value:
            path = Path(value).expanduser()
            tls[key] = str(path if path.is_absolute() else (base / path).resolve())
    return result


def effective_config(args, environ):
    report, connection, document = require_context(environ, args)
    route = load_route(args)
    if route.get("context_revision") != report["revision"]:
        raise projects.ProjectError("FRP 来源已变化；重新检查并保存本连接路由后再启动。",
                                    code="context_changed", exit_code=5)
    if route["domain"] != route["domain_prefix"] + "." + report["domain_suffix"]["value"]:
        raise projects.ProjectError("实例域名与当前 FRP 父域不一致。", code="context_changed", exit_code=5)
    # Source-relative certificate paths are resolved before merging. Never copy
    # project proxies, visitors, includes or its existing fixed-domain record.
    base = _globals(connection.defaults, Path(connection.source).parent)
    auth = connection.defaults.get("auth", {})
    base["auth"] = copy.deepcopy(auth.unwrap() if hasattr(auth, "unwrap") else auth)
    connection.defaults = base
    client = _globals(document["client"], args.project)
    if route.get("require_tls"):
        for settings in (base, client):
            if settings.get("transport", {}).get("tls", {}).get("enable") is False:
                raise projects.ProjectError("本连接要求 FRP TLS，但生效来源显式禁用。", code="tls_required")
        client.setdefault("transport", {}).setdefault("tls", {})["enable"] = True
    client["proxies"] = [route["proxy"]]
    return projects.effective_config(connection, {"client": client}), connection, route
