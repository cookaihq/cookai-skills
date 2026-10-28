"""Project connection selection, persistent routes and local domain ownership.

No network requests or process control live here. Secrets are written only to the
caller's .env.local; the native frpc configuration is assembled in memory.
"""
from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from pathlib import Path

import tomlkit


PREFIX = "FRPC_LAUNCH_"
CONNECTION_KEYS = (PREFIX + "SERVER_ADDR", PREFIX + "SERVER_PORT", PREFIX + "TOKEN")
SUFFIX_KEY = PREFIX + "DOMAIN_SUFFIX"
RUNTIME_ENV = PREFIX + "RUNTIME_CONFIG"
RUNTIME_TEMPLATE = "{{ .Envs." + RUNTIME_ENV + " }}\n"
ENV_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$")


class ProjectError(Exception):
    def __init__(self, message, *, code="invalid_config", source="", missing=(), exit_code=1):
        super().__init__(message)
        self.code, self.source, self.missing = code, source, list(missing)
        self.exit_code = exit_code

    def report(self):
        return {"result": self.code, "detail": str(self), "source": self.source,
                "missing": self.missing, "exit_code": self.exit_code}


def read_text(path):
    try:
        return path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeError):
        raise ProjectError("配置无法读取：%s" % path, source=str(path)) from None


def parse_env(text):
    result = {}
    for line in text.splitlines():
        match = ENV_LINE.match(line)
        if match:
            key, value = match.groups()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key] = value
    return result


def env_layers(environ, project, defaults):
    # Read lazily: an unrelated broken global file cannot replace a complete
    # higher priority connection or expose another project's credentials.
    yield "env", environ
    for filename in (".env.frpc-launch", ".env.local", ".env"):
        path = project / filename
        yield str(path), parse_env(read_text(path))
    path = defaults / ".env"
    yield str(path), parse_env(read_text(path))


def scalar(name, environ, project, defaults):
    for source, values in env_layers(environ, project, defaults):
        if values.get(name):
            return values[name], source
    return "", ""


def load_toml(path):
    text = read_text(path)
    try:
        return tomlkit.parse(text)
    except Exception:
        # TOML parser errors may quote the offending secret line.
        raise ProjectError("TOML 无法解析：%s（未回显文件内容）" % path,
                           source=str(path)) from None


@dataclasses.dataclass(repr=False)
class Connection:
    addr: str
    port: int
    token: str
    source: str
    defaults: dict = dataclasses.field(default_factory=dict)

    def report(self):
        return {"source": self.source, "fields": {
            key: {"source": self.source, "configured": True, "sensitive": index == 2}
            for index, key in enumerate(CONNECTION_KEYS)}}


def connection_group(values, source, defaults=None):
    if not any(values.get(key) not in (None, "") for key in CONNECTION_KEYS):
        return None
    missing = [key for key in CONNECTION_KEYS if values.get(key) in (None, "")]
    if missing:
        raise ProjectError("连接组不完整；请在同一来源补齐地址、端口和 Token。",
                           code="incomplete_connection", source=source,
                           missing=missing, exit_code=4)
    addr, port, token = (values[key] for key in CONNECTION_KEYS)
    if not isinstance(addr, str) or not addr.strip() or any(c.isspace() for c in addr):
        raise ProjectError("serverAddr 必须是非空主机名或 IP，不能包含空白。", source=source)
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n\0"):
        raise ProjectError("Token 必须为非空单行字符串。", source=source)
    if isinstance(port, bool) or not re.fullmatch(r"[0-9]+", str(port)) or not 1 <= int(port) <= 65535:
        raise ProjectError("serverPort 必须是 1–65535 的整数。", source=source)
    return Connection(addr, int(port), token, source, defaults or {})


def resolve_connection(environ, project, defaults, request=None):
    if request:
        selected = connection_group(request, "request")
        if selected:
            return selected
    for source, values in env_layers(environ, project, defaults):
        selected = connection_group(values, source)
        if selected:
            return selected
    path = defaults / "frpc.toml"
    document = load_toml(path)
    if document:
        auth = document.get("auth", {})
        if not isinstance(auth, dict) or auth.get("method", "token") != "token":
            raise ProjectError("项目连接流程需要 token 认证；请核对实际认证方式。", source=str(path))
        selected = connection_group(dict(zip(CONNECTION_KEYS, (
            document.get("serverAddr"), document.get("serverPort"), auth.get("token")))),
            str(path), document)
        if selected:
            return selected
    raise ProjectError("未找到完整 FRPS 连接；先保存连接信息，本地端口可在使用时提供。",
                       code="unconfigured", missing=CONNECTION_KEYS, exit_code=4)


def project_id(project):
    canonical = os.path.normcase(str(project.resolve()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def runtime_home(project, defaults):
    return defaults / "projects" / project_id(project)


def config_path(project):
    return project / ".frpc-launch" / "project.toml"


def load_project(project):
    path = config_path(project)
    document = load_toml(path)
    if path.exists():
        if document.get("schema") != 1 or not isinstance(document.get("client"), dict):
            raise ProjectError("项目记录格式无效：%s" % path, source=str(path))
        if document.get("domain"):
            validate_domain(document["domain"])
    else:
        document["schema"] = 1
        document["client"] = tomlkit.table()
    validate_client(document["client"])
    return document


def validate_client(client):
    for key in ("serverAddr", "serverPort", "auth"):
        if key in client:
            raise ProjectError("项目 client 不保存 %s；连接组请放在 .env.local。" % key)
    # Native advanced options are preserved, but a second project secret store
    # would defeat the .env.local boundary. Leave such deployments on the native
    # full-configuration interface until explicitly migrated.
    def inspect(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if re.search(r"token|password|secret", str(key), re.I) and item:
                    raise ProjectError("项目 client 含凭证字段；请核对 Secret 保存方式。")
                inspect(item)
        elif isinstance(value, list):
            for item in value:
                inspect(item)
    inspect(client)
    proxies = client.get("proxies", [])
    if not isinstance(proxies, list):
        raise ProjectError("client.proxies 必须是代理列表。")
    names = [p.get("name") for p in proxies if isinstance(p, dict)]
    if len(names) != len(proxies) or any(not isinstance(n, str) or not n for n in names) or len(set(names)) != len(names):
        raise ProjectError("项目代理必须有唯一的非空名称。")


def validate_domain(value):
    if not isinstance(value, str):
        raise ProjectError("域名必须是字符串。")
    try:
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ProjectError("域名格式无效。") from None
    labels = value.split(".")
    if len(value) > 253 or len(labels) < 2 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in labels):
        raise ProjectError("请提供完整主机名，不包含协议、端口、路径或通配符。")
    return value


def project_slug(name):
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")
    return (slug or "project-" + hashlib.sha256(name.encode()).hexdigest()[:8])[:63].rstrip("-")


def read_registry(defaults):
    path = defaults / "domains.json"
    text = read_text(path)
    try:
        result = json.loads(text) if text else {}
        if not isinstance(result, dict) or any(not isinstance(v, str) for v in result.values()):
            raise ValueError()
        return result
    except ValueError:
        raise ProjectError("本地域名登记损坏：%s；请核对，未自动清空。" % path) from None


def select_domain(document, project, suffix, explicit, registry, replace=False):
    owner = os.path.normcase(str(project.resolve()))
    existing = document.get("domain", "")
    explicit = validate_domain(explicit) if explicit else ""
    if existing and explicit and existing != explicit and not replace:
        raise ProjectError("项目已固定域名 %s；变更需明确使用 --replace-domain。" % existing,
                           code="domain_fixed")
    candidate = explicit if replace or not existing else existing
    candidate = candidate or existing
    generated = not candidate
    if generated:
        if not suffix:
            return ""
        suffix = validate_domain(suffix)
        candidate = validate_domain(project_slug(project.name) + "." + suffix)
    if registry.get(candidate, owner) != owner:
        if not generated:
            raise ProjectError("域名 %s 已由另一个本地项目登记：%s" % (candidate, registry[candidate]),
                               code="domain_conflict")
        slug = project_slug(project.name)[:54].rstrip("-") + "-" + project_id(project)[:8]
        candidate = validate_domain(slug + "." + suffix)
        if registry.get(candidate, owner) != owner:
            raise ProjectError("派生域名仍有冲突，请明确指定新域名。", code="domain_conflict")
    if existing and existing != candidate and registry.get(existing) == owner:
        del registry[existing]
    registry[candidate] = owner
    document["domain"] = candidate
    return candidate


def upsert_proxies(document, updates):
    client = document["client"]
    proxies = client.setdefault("proxies", tomlkit.aot())
    seen = set()
    for update in updates:
        name = update["name"]
        if name in seen:
            raise ProjectError("本次重复指定代理 %s。" % name)
        seen.add(name)
        current = next((p for p in proxies if p.get("name") == name), None)
        if current is None:
            proxies.append(update)
        else:
            if current.get("type") != update.get("type"):
                raise ProjectError("代理 %s 已存在且类型不同；请核对原生配置，不自动改变协议。" % name)
            for key, value in update.items():
                current[key] = value
    validate_client(client)


def effective_config(connection, document):
    client = document["client"]
    validate_client(client)
    if not client.get("proxies"):
        raise ProjectError("连接已就绪，但当前项目尚无代理；请在使用时提供本地地址和端口。",
                           code="no_project_proxy", exit_code=4)
    # Never copy another project's proxies, visitors or credential objects.
    base = connection.defaults.unwrap() if hasattr(connection.defaults, "unwrap") else connection.defaults
    result = {key: copy.deepcopy(value) for key, value in base.items()
              if key in ("transport", "user", "metadatas", "dnsServer", "loginFailExit")}
    def merge(target, overlay):
        for key, value in overlay.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = copy.deepcopy(value)
    merge(result, client.unwrap() if hasattr(client, "unwrap") else client)
    if result.get("includes"):
        raise ProjectError("项目配置不自动加载 includes；请将已核对的代理保存在本项目。")
    result["serverAddr"], result["serverPort"] = connection.addr, connection.port
    result["auth"] = copy.deepcopy(base.get("auth", {}))
    result["auth"].update({"method": "token", "token": connection.token})
    result.setdefault("log", {})["to"] = "console"
    return tomlkit.dumps(result)


def updated_env(original, updates):
    for value in updates.values():
        if any(c in str(value) for c in "\r\n\0"):
            raise ProjectError("配置值必须为单行，未写入文件。")
    output, written = [], set()
    for line in original.splitlines():
        match = ENV_LINE.match(line)
        key = match.group(1) if match else ""
        if key in updates:
            if key not in written:
                output.append(env_assignment(key, updates[key]))
                written.add(key)
        else:
            output.append(line)
    output += [env_assignment(key, value) for key, value in updates.items() if key not in written]
    return "\n".join(output) + "\n"


def env_assignment(key, value):
    value = str(value)
    if value != value.strip() or (value and value[0] in "\"'"):
        value = '"' + value + '"'
    return key + "=" + value


def secret_target_check(project, target):
    if target.is_symlink() or target.resolve().parent != project.resolve():
        raise ProjectError("Secret 目标必须是当前项目内的普通 .env.local 文件。", exit_code=6)
    def git(*args):
        try:
            return subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                                  text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise ProjectError("无法完成 Git 安全检查，未写入 Secret。", exit_code=6) from None
    root = git("rev-parse", "--show-toplevel")
    if root.returncode:
        # Git distinguishes non-repositories from an inaccessible repository.
        if "not a git repository" in root.stderr.lower():
            return
        raise ProjectError("无法确认项目 Git 状态，未写入 Secret。", exit_code=6)
    tracked = git("ls-files", "--error-unmatch", "--", str(target))
    if tracked.returncode == 0:
        raise ProjectError(".env.local 已被 Git 跟踪，拒绝保存 Secret。", exit_code=6)
    if tracked.returncode != 1 or git("check-ignore", "-q", "--", str(target)).returncode != 0:
        raise ProjectError(".env.local 未确认被 Git 忽略；先修正忽略规则。", exit_code=6)


def atomic_write(path, text):
    if path.is_symlink():
        raise ProjectError("拒绝覆盖符号链接：%s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextlib.contextmanager
def operation_lock(path, timeout=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ProjectError("同一项目有另一项操作正在执行，请稍后重试。", code="busy")
                time.sleep(0.1)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save_project(project, defaults, connection, document, registry, persist_connection=False):
    path = config_path(project)
    if path.parent.is_symlink():
        raise ProjectError(".frpc-launch 必须是当前项目内的普通目录。")
    changes = []
    if persist_connection:
        env_path = project / ".env.local"
        secret_target_check(project, env_path)
        dedicated = project / ".env.frpc-launch"
        higher = connection_group(parse_env(read_text(dedicated)), str(dedicated))
        if higher and (higher.addr, higher.port, higher.token) != (connection.addr, connection.port, connection.token):
            raise ProjectError(".env.frpc-launch 中已有更高优先级连接；先修复实际来源，"
                               "不能写 .env.local 假装覆盖成功。", code="save_shadowed",
                               source=str(dedicated))
        changes.append((env_path, updated_env(read_text(env_path), dict(zip(
            CONNECTION_KEYS, (connection.addr, connection.port, connection.token))))))
    changes.append((path, tomlkit.dumps(document)))
    changes.append((defaults / "domains.json", json.dumps(registry, ensure_ascii=False, indent=2) + "\n"))
    before = [(target, target.read_bytes() if target.exists() else None) for target, _ in changes]
    applied = []
    try:
        for target, text in changes:
            atomic_write(target, text)
            applied.append(target)
    except Exception:
        for target, previous in reversed(before):
            if target in applied:
                if previous is None:
                    target.unlink(missing_ok=True)
                else:
                    atomic_write(target, previous.decode("utf-8"))
        raise
    return [str(target) for target, _ in changes]


def request_connection(server_addr, server_port, environ):
    """CLI address/port + process-only initial token form one explicit request."""
    token = environ.get(PREFIX + "INIT_TOKEN", "")
    if server_addr or server_port or token:
        return dict(zip(CONNECTION_KEYS, (server_addr, server_port or "", token)))
    return None


def default_proxy_name(project):
    return "project-" + project_id(project)[:12] + "-web"


def local_proxy(document, project, port, *, name="", local_ip="", proxy_type="",
                route_mode="", subdomain_host="", locations=None):
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ProjectError("本地端口必须是 1–65535 的整数。")
    name = name or default_proxy_name(project)
    existing = next((p for p in document["client"].get("proxies", []) if p.get("name") == name), {})
    kind = proxy_type or existing.get("type", "http")
    if kind not in ("http", "https"):
        raise ProjectError("本地网站快捷配置只接受 http/https；TCP/UDP 请提供完整代理规格。")
    update = {"name": name, "type": kind, "localPort": int(port)}
    if local_ip or not existing:
        update["localIP"] = local_ip or "127.0.0.1"
    if route_mode or not existing:
        domain = document.get("domain", "")
        if not domain:
            raise ProjectError("请先核对域名后缀或完整项目域名。", code="domain_required", exit_code=4)
        if route_mode == "customDomains":
            if existing.get("subdomain"):
                raise ProjectError("现有代理使用 subdomain；请核对原生配置后变更路由模式。")
            update["customDomains"] = [domain]
        elif route_mode == "subdomain":
            host = validate_domain(subdomain_host) if subdomain_host else ""
            label, suffix = domain.split(".", 1)
            if suffix != host:
                raise ProjectError("subdomain 路由需要已核实且与项目域名匹配的 --subdomain-host。",
                                   code="subdomain_host_unconfirmed", exit_code=4)
            if existing.get("customDomains"):
                raise ProjectError("现有代理使用 customDomains；请核对原生配置后变更路由模式。")
            update["subdomain"] = label
        else:
            raise ProjectError("先核对 FRPS 域名路由，再指定 --route-mode customDomains 或 subdomain。",
                               code="route_unconfirmed", exit_code=4)
    if locations is not None:
        if kind != "http" or not locations or any(not value.startswith("/") for value in locations):
            raise ProjectError("locations 只用于 HTTP，并且每条路径都要以 / 开头。")
        update["locations"] = locations
    return update


def project_report(project, defaults, connection, document):
    return {"project": str(project), "project_id": project_id(project),
            "runtime_dir": str(runtime_home(project, defaults)),
            "project_file": str(config_path(project)), "domain": document.get("domain", ""),
            "proxies": [{key: value for key, value in proxy.items() if key in (
                "name", "type", "localIP", "localPort", "remotePort", "customDomains", "subdomain", "locations")}
                for proxy in document["client"].get("proxies", [])],
            "connection": connection.report(), "config_source": connection.source,
            "uses_global_connection": connection.source in (str(defaults / ".env"), str(defaults / "frpc.toml")),
            "public_endpoint": "not_checked"}


def configure(args, environ, parse_proxy):
    """Apply an authorized project configuration; never starts a tunnel."""
    project, defaults = args.project.resolve(), args.home.resolve()
    if not project.is_dir():
        raise ProjectError("项目目录不存在：%s" % project)
    with operation_lock(runtime_home(project, defaults) / "operation.lock"), operation_lock(defaults / "domains.lock"):
        request = request_connection(args.server_addr, args.server_port, environ)
        connection = resolve_connection(environ, project, defaults, request)
        document, registry = load_project(project), read_registry(defaults)
        suffix = args.domain_suffix or scalar(SUFFIX_KEY, environ, project, defaults)[0]
        original_domain = document.get("domain", "")
        domain = select_domain(document, project, suffix, args.domain, registry, args.replace_domain)
        if original_domain and domain != original_domain:
            for proxy in document["client"].get("proxies", []):
                if original_domain in proxy.get("customDomains", []):
                    proxy["customDomains"] = [domain if d == original_domain else d for d in proxy["customDomains"]]
                if proxy.get("subdomain") == original_domain.split(".")[0]:
                    if domain.split(".", 1)[1] != args.subdomain_host:
                        raise ProjectError("变更 subdomain 域名需重新核对 --subdomain-host。")
                    proxy["subdomain"] = domain.split(".")[0]
        updates = []
        if args.local_port is not None:
            updates.append(local_proxy(document, project, args.local_port, name=args.proxy_name,
                                       local_ip=args.local_ip, proxy_type=args.proxy_type,
                                       route_mode=args.route_mode, subdomain_host=args.subdomain_host,
                                       locations=args.locations))
        for spec in args.proxies:
            proxy = parse_proxy(spec)
            for key in ("customDomains", "locations"):
                if key in proxy:
                    proxy[key] = [v.strip() for v in proxy[key].split(",")]
            for key in ("localPort", "remotePort"):
                if key in proxy:
                    proxy[key] = int(proxy[key])
            updates.append(proxy)
        if updates:
            upsert_proxies(document, updates)
        if document["client"].get("proxies"):
            effective_config(connection, document)  # validate before any target is replaced
        persist = connection.source in ("request", "env")
        written = save_project(project, defaults, connection, document, registry, persist)
        # Re-read the authoritative files for the next invocation, without this
        # call's environment. A complete explicit group must be reusable there.
        saved = resolve_connection({}, project, defaults) if persist else connection
        result = project_report(project, defaults, saved, load_project(project))
        result.update({"result": "configured", "written": written,
                       "input_source": connection.source, "connection_saved": persist})
        return result
