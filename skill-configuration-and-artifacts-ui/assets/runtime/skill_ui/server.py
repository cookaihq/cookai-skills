import hmac
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
from pathlib import Path
import re
import secrets
import threading
import time
import traceback
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .contracts import UIError, public_config
from .storage import atomic_json

STATIC = Path(__file__).resolve().parent / "static"


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, adapter, project, page, directory, idle_seconds=3600, public_origin=None):
        if page not in ("configuration", "artifacts") or idle_seconds <= 0:
            raise ValueError("Invalid page or lifetime")
        super().__init__(("127.0.0.1", 0), Handler)
        self.adapter, self.project, self.page, self.directory = adapter, project, page, directory
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.public_origin = public_origin or self.origin
        parsed = urlsplit(self.public_origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            self.server_close()
            raise ValueError("Public origin must be an exact http(s) origin without a path")
        self.origins = {self.origin, self.public_origin}
        self.instance = secrets.token_hex(16)
        self.token, self.cookie, self.csrf = (secrets.token_urlsafe(32) for _ in range(3))
        self.cookie_name = "skill_ui_" + self.instance[:16]
        self.idle_seconds, self.last_use = idle_seconds, time.monotonic()
        self.resident, self.ended = False, False
        self.lock, self.closed = threading.RLock(), threading.Event()

    def lifetime(self, renew=False, resident=None):
        with self.lock:
            left = self.idle_seconds - (time.monotonic() - self.last_use)
            if self.ended or (not self.resident and left <= 0):
                raise UIError("expired", "此页面已结束或到期，请让 Agent 重新打开。", 410)
            if resident is not None:
                if self.page != "artifacts" or type(resident) is not bool:
                    raise UIError("lifetime", "仅产物页面支持本次服务常驻。")
                self.resident = resident
            if renew or resident is not None:
                self.last_use, left = time.monotonic(), self.idle_seconds
            return {"resident": self.resident, "remaining": None if self.resident else math.ceil(left),
                    "idle_seconds": self.idle_seconds}

    def stop(self):
        with self.lock:
            self.ended = True
        threading.Thread(target=self.shutdown, daemon=True).start()

    def watcher(self):
        while not self.closed.wait(0.2):
            try:
                self.lifetime()
            except UIError:
                logging.info("service_expired page=%s instance=%s", self.page, self.instance)
                self.stop()
                return


class Handler(BaseHTTPRequestHandler):
    server_version = "SkillUI"

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # URL/body/cookie may contain secrets. Events are logged explicitly.

    def headers_for(self, code, length, mime="application/json; charset=utf-8", extra=None):
        self.send_response(code)
        headers = {"Content-Type": mime, "Content-Length": str(length), "Cache-Control": "no-store",
                   "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                   "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"}
        for key, value in {**headers, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()

    def send(self, code, data, mime="application/json; charset=utf-8", extra=None):
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode() if isinstance(data, dict) else data
        self.headers_for(code, len(raw), mime, extra)
        self.wfile.write(raw)

    def check_origin(self, post=False):
        hosts = {urlsplit(origin).netloc for origin in self.server.origins}
        host = self.headers.get("Host")
        if host not in hosts:
            raise UIError("host", "请使用 Agent 提供的页面地址。", 403)
        if post:
            origin = self.headers.get("Origin", "")
            if origin not in self.server.origins or urlsplit(origin).netloc != host:
                raise UIError("origin", "页面地址与请求不匹配。", 403)

    def authorized(self):
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get(self.server.cookie_name)
            if cookie and hmac.compare_digest(cookie.value, self.server.cookie):
                return
        except Exception:
            pass
        raise UIError("session", "请从 Agent 提供的完整链接重新打开页面。", 401)

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise UIError("body", "需要 JSON 请求。", 415)
        length = self.headers.get("Content-Length", "")
        if not length.isdigit() or int(length) > 1024 * 1024:
            raise UIError("body", "请求大小无效。", 413)
        try:
            value = json.loads(self.rfile.read(int(length)), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeError):
            raise UIError("body", "请求内容格式无效。") from None

    def state(self):
        server = self.server
        with server.lock:
            data = {"page": server.page, "title": server.adapter.title, "project": str(server.project),
                    "lifetime": server.lifetime(), "csrf": server.csrf, "instance": server.instance}
            if server.page == "configuration":
                data["config"] = public_config(server.adapter.config())
            else:
                data["artifacts"] = server.adapter.artifacts()
                data["extensions"] = [name for name in server.adapter.extensions() if name.endswith(".js")]
            return data

    def resource(self, resource_id, download):
        if self.server.page != "artifacts":
            raise UIError("missing", "配置页面不提供产物资源。", 404)
        with self.server.lock:
            resource = self.server.adapter.resource(resource_id)
        stream = None
        try:
            if isinstance(resource.content, Path):
                stream = resource.content.open("rb")
                length = resource.content.stat().st_size
            else:
                import io
                stream = io.BytesIO(resource.content)
                length = len(resource.content)
            start, end, code = 0, length - 1, 200
            extra = {"Accept-Ranges": "bytes"}
            if self.headers.get("Range"):
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers["Range"])
                if not match or not any(match.groups()) or length == 0:
                    return self.send(416, b"", extra={"Content-Range": f"bytes */{length}"})
                if not match[1]:
                    start = max(0, length - int(match[2]))
                else:
                    start, end = int(match[1]), min(int(match[2]) if match[2] else end, end)
                if start > end:
                    return self.send(416, b"", extra={"Content-Range": f"bytes */{length}"})
                code = 206
                extra["Content-Range"] = f"bytes {start}-{end}/{length}"
            mime = resource.mime
            safe = mime.startswith(("image/", "audio/", "video/", "text/plain"))
            if download or not safe:
                extra["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(resource.name, safe="")
            # Even an SVG opened as a document cannot execute script or navigate its opener.
            extra["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
            self.headers_for(code, max(0, end-start+1), mime, extra)
            stream.seek(start)
            left = end-start+1
            while left > 0:
                chunk = stream.read(min(left, 65536))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)
        except FileNotFoundError:
            raise UIError("missing", "文件已移动或不存在，记录仍保留。", 404) from None
        finally:
            if stream:
                stream.close()

    def get(self):
        self.check_origin()
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            return self.send(200, {"app": "skill-ui", "instance": self.server.instance, "page": self.server.page,
                                   "lifetime": self.server.lifetime()})
        static = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8"),
                  "/configuration.js": ("configuration.js", "text/javascript; charset=utf-8"),
                  "/configuration.css": ("configuration.css", "text/css; charset=utf-8")}
        if parsed.path in static:
            name, mime = static[parsed.path]
            return self.send(200, (STATIC / name).read_bytes(), mime)
        self.authorized()
        self.server.lifetime()
        if parsed.path == "/api/state":
            return self.send(200, self.state())
        if parsed.path == "/api/lifetime":
            return self.send(200, self.server.lifetime())
        if parsed.path.startswith("/resources/"):
            return self.resource(unquote(parsed.path[len("/resources/"):]), "download" in parse_qs(parsed.query))
        if parsed.path.startswith("/extensions/") and self.server.page == "artifacts":
            name = parsed.path[len("/extensions/"):]
            registered = self.server.adapter.extensions()
            if name in registered and re.fullmatch(r"[a-zA-Z0-9_.-]+\.(js|css)", name):
                mime = "text/javascript" if name.endswith(".js") else "text/css"
                return self.send(200, Path(registered[name]).read_bytes(), mime + "; charset=utf-8")
        raise UIError("missing", "此入口不存在。", 404)

    def post(self):
        self.check_origin(post=True)
        data = self.body()
        server = self.server
        path = urlsplit(self.path).path
        server.lifetime()
        if path in ("/api/session", "/api/control/stop"):
            if not isinstance(data.get("token"), str) or not hmac.compare_digest(data["token"], server.token):
                raise UIError("token", "访问链接无效。", 403)
            if path.endswith("/stop"):
                self.send(200, {"stopped": True})
                return server.stop()
            secure = "; Secure" if self.headers["Origin"].startswith("https:") else ""
            cookie = f"{server.cookie_name}={server.cookie}; HttpOnly; SameSite=Strict; Path=/" + secure
            return self.send(200, {"csrf": server.csrf}, extra={"Set-Cookie": cookie})
        self.authorized()
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), server.csrf):
            raise UIError("csrf", "请求校验失败，请重新打开页面。", 403)
        if path == "/api/activity":
            return self.send(200, server.lifetime(renew=True))
        if path == "/api/residency":
            return self.send(200, server.lifetime(resident=data.get("resident")))
        if path == "/api/stop":
            self.send(200, {"stopped": True})
            return server.stop()
        if server.page == "configuration":
            with server.lock:
                if path == "/api/reveal":
                    snapshot = server.adapter.config()
                    if snapshot["revision"] != data.get("revision"):
                        raise UIError("conflict", "配置已变化，请重新载入核对。", 409)
                    for field in snapshot["fields"]:
                        if field["id"] == data.get("id") and field.get("sensitive"):
                            record = field["effective"] if data.get("target") is None else next((r for r in field["rows"] if r["layer"] == data["target"]), None)
                            if record is None:
                                raise UIError("target", "请选择有效的配置文件。")
                            return self.send(200, {"value": record.get("value", "")})
                    raise UIError("field", "此字段不支持查看原值。")
                if path == "/api/preview":
                    snapshot = server.adapter.preview(data.get("changes"), data.get("revision"), data.get("target"))
                    return self.send(200, {"config": public_config(snapshot)})
                if path == "/api/save":
                    snapshot = server.adapter.save(data.get("changes"), data.get("revision"), data.get("target"))
                    public = public_config(snapshot)
                    receipt = {"instance": server.instance, "project": str(server.project), "revision": snapshot["revision"],
                               "task": snapshot.get("task"), "validation": snapshot.get("validation", "适配器校验"),
                               "target": data.get("target"), "saved_at": time.time()}
                    atomic_json(server.directory / "saved.json", receipt, owner_path=server.project)
                    logging.info("config_saved instance=%s", server.instance)
                    return self.send(200, {"config": public, "receipt": receipt})
        raise UIError("missing", "此操作不存在。", 404)

    def dispatch(self, action):
        try:
            action()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except UIError as exc:
            logging.warning("request_rejected page=%s code=%s", self.server.page, exc.code)
            self.send(exc.status, {"error": exc.code, "message": exc.message})
        except Exception as exc:
            # Keep type + frames for diagnosis, omit exception text and locals (may contain secrets).
            frames = ";".join(f"{Path(f.filename).name}:{f.lineno}:{f.name}" for f in traceback.extract_tb(exc.__traceback__))
            logging.error("request_failed type=%s frames=%s", type(exc).__name__, frames)
            self.send(500, {"error": "internal", "message": "读取或操作失败，请重试；详细位置已记录在服务日志。"})

    def do_GET(self):
        self.dispatch(self.get)

    def do_POST(self):
        self.dispatch(self.post)
