"""Authenticated local profile discovery for the existing Desktop gateway.

Uses native protocol-v1 tickets, one per GET; never publishes a token, reads
chats, switches profiles or starts another Hermes process. Legacy dashboards
without this protocol return an explicit unsupported/unavailable result.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error

import psutil

from discovery import discover_live, candidates, observe_bounded, ObservationFailure
from native_control import ControlError, process_home, query, verify_connected_peer

PROFILE_BUDGET_SECONDS = 35
HTTP_TIMEOUT_SECONDS = 3
MAX_HTTP_BYTES = 512 * 1024
MAX_PROFILES = 256
ALLOWED_PATHS = ("/api/profiles", "/api/profiles/active")


def unavailable(reason, **extra):
    detail = {
        "backend_user_mismatch": "当前执行账号与 Hermes 所属账号不同；接入组件须由 Hermes 所属账号运行。",
        "native_control_access_denied": "当前执行环境无权读取 Hermes 原生身份通道或进程信息。",
        "backend_identity_changed": "检查期间 Hermes 进程或监听身份发生变化；请重新发现后端。",
    }.get(reason)
    return {"result": "profiles_unavailable", "reason": reason,
            **({"detail": detail} if detail else {}), "profiles": [],
            "publication": "not_performed", **extra}


def path_key(path):
    if not isinstance(path, str) or len(path) > 4096 or any(c in path for c in "\r\n\0") or not Path(path).is_absolute():
        raise ControlError("profile_path_invalid")
    return os.path.normcase(str(Path(path).resolve()))


def safe_name(value):
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ControlError("profile_schema_invalid")
    return value


def checked_identity(identity, candidate, home):
    authority = f'[{candidate["host"]}]' if ":" in candidate["host"] else candidate["host"]
    origin = f'http://{authority}:{candidate["port"]}'
    if (not isinstance(identity, dict) or type(identity.get("protocol")) is not int
            or type(identity.get("runtime_protocol")) is not int
            or identity.get("protocol") != 1 or identity.get("runtime_protocol") != 1
            or identity.get("kind") != "hermes-gateway" or identity.get("pid") != candidate["pid"]
            or identity.get("api_origin") != origin or identity.get("code_sha") != candidate["commit"]
            or identity.get("state") != "ready" or path_key(identity.get("hermes_home")) != path_key(home)
            or not isinstance(identity.get("instance_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity["instance_id"])):
        raise ControlError("native_identity_mismatch")
    rows = identity.get("served_profiles")
    if not isinstance(rows, list) or not rows or len(rows) > MAX_PROFILES:
        raise ControlError("served_profiles_unavailable")
    served = set()
    for row in rows:
        if not isinstance(row, dict) or path_key(row.get("profile_id")) != path_key(row.get("home")):
            raise ControlError("served_profiles_unavailable")
        key = path_key(row["home"])
        if key in served:
            raise ControlError("served_profiles_unavailable")
        served.add(key)
    if path_key(home) not in served:
        raise ControlError("native_identity_mismatch")
    return origin, served


def checked_profiles(roster, active, served):
    rows = roster.get("profiles") if isinstance(roster, dict) else None
    if not isinstance(rows, list) or not rows or len(rows) > MAX_PROFILES or not isinstance(active, dict):
        raise ControlError("profile_schema_invalid")
    profiles, names, paths = [], set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ControlError("profile_schema_invalid")
        name, path = safe_name(row.get("name")), row.get("path")
        key = path_key(path)
        if name in names or key in paths or type(row.get("is_default")) is not bool:
            raise ControlError("profile_schema_invalid")
        names.add(name)
        paths.add(key)
        profiles.append({"name": name, "path": str(Path(path).resolve()),
                         "is_default": row["is_default"], "served": key in served})
    # Every served home must have an unambiguous profile entry. A roster-only
    # profile can be displayed diagnostically but cannot be selected for binding.
    if not served <= paths:
        raise ControlError("profile_roster_changed")
    return profiles, safe_name(active.get("active")), safe_name(active.get("current"))


def http_get(origin, path, ticket, *, candidate):
    if path not in ALLOWED_PATHS:
        raise ControlError("profile_path_not_allowed")
    authority = f'[{candidate["host"]}]' if ':' in candidate['host'] else candidate['host']
    if origin != f'http://{authority}:{candidate["port"]}':
        raise ControlError('upstream_peer_mismatch')

    class OwnedConnection(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            try:
                # No HTTP bytes have been sent. Allow only the brief OS table
                # visibility delay; never retry the request or its native grant.
                for attempt in range(3):
                    try:
                        verify_connected_peer(candidate, self.sock.getsockname(), self.sock.getpeername())
                        return
                    except ControlError as exc:
                        if str(exc) != 'upstream_peer_owner_unverified' or attempt == 2:
                            raise
                        time.sleep(0.025)
            except BaseException:
                self.close()
                raise

    # A direct connection has no proxy, cookie jar or redirect handling.
    connection = OwnedConnection(candidate['host'], candidate['port'], timeout=HTTP_TIMEOUT_SECONDS)
    try:
        connection.request('GET', path, headers={'X-Hermes-Gateway-Ticket': ticket})
        response = connection.getresponse()
        if response.status != 200:
            raise urllib.error.HTTPError(origin + path, response.status, 'profile_http_rejected', None, None)
        raw = response.read(MAX_HTTP_BYTES + 1)
        if len(raw) > MAX_HTTP_BYTES:
            raise ControlError("profile_response_invalid")
        return json.loads(raw)
    finally:
        connection.close()


def inspect_profiles(candidate, home, *, control=query, get=None, stable):
    if not stable():
        raise ControlError("backend_identity_changed")
    identity = control(home, candidate)
    origin, served = checked_identity(identity, candidate, home)

    def read(path):
        if not stable():
            raise ControlError("backend_identity_changed")
        grant = control(home, candidate, "session-ticket", {
            "profile_id": home, "instance_id": identity["instance_id"], "purpose": "native-http"})
        ticket = grant.get("ticket")
        if (grant.get("instance_id") != identity["instance_id"] or grant.get("profile_id") != home
                or grant.get("runtime_protocol") != 1 or not isinstance(ticket, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", ticket)):
            raise ControlError("native_ticket_mismatch")
        # No retries: each credential is consumed by one request. The worker
        # returns only the allowlisted profile projection, never this grant.
        if get is not None:
            return get(origin, path, ticket)
        return http_get(origin, path, ticket, candidate=candidate)

    profiles, active, current = checked_profiles(read(ALLOWED_PATHS[0]), read(ALLOWED_PATHS[1]), served)
    latest = control(home, candidate)
    latest_origin, latest_served = checked_identity(latest, candidate, home)
    if (not stable() or latest["instance_id"] != identity["instance_id"]
            or latest_served != served or latest_origin != origin):
        raise ControlError("backend_identity_changed")
    result = {"result": "profiles_verified", "candidate": candidate["id"], "pid": candidate["pid"],
              "home": home, "instance_id": identity["instance_id"], "version": candidate["version"],
              "commit": candidate["commit"], "profiles": profiles, "active": active, "current": current,
              "authentication": "native_http_single_use_ticket", "rest_authenticated": "profiles_only",
              "publication": "not_performed", "websocket": "not_checked", "android": "not_checked"}
    result["revision"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return result


def worker(selected=None):
    report = discover_live(selected=selected)
    if not report.get("selected"):
        return unavailable(report["state"])
    candidate = next(row for row in report["candidates"] if row["id"] == report["selected"])
    home = process_home(candidate)

    def stable():
        observation = observe_bounded(4)
        # Desktop descendants include short-lived tools unrelated to the
        # selected Gateway. Their exit does not change an exact, still-present
        # Gateway/Desktop generation; all other observation failures block.
        return (not (set(observation.issues) - {'candidate_exited'})
                and any(row["id"] == candidate["id"] for row in candidates(observation)))

    return inspect_profiles(candidate, home, stable=stable)


def read_profiles(*, selected=None):
    if selected is not None and (not isinstance(selected, str) or not re.fullmatch(r"[0-9a-f]{24}", selected)):
        return unavailable("invalid_candidate")
    argv = [sys.executable, "-B", str(Path(__file__).resolve()), "--worker"]
    if selected:
        argv.append(selected)
    try:
        process = subprocess.run(argv, capture_output=True, timeout=PROFILE_BUDGET_SECONDS,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        data = json.loads(process.stdout)
        if (not isinstance(data, dict) or data.get("result") not in ("profiles_verified", "profiles_unavailable")
                or (process.returncode and data["result"] == "profiles_verified")):
            return unavailable("profile_worker_failed")
        return data
    except subprocess.TimeoutExpired:
        return unavailable("profile_query_timeout")
    except (OSError, ValueError):
        return unavailable("profile_worker_failed")


if __name__ == "__main__" and sys.argv[1:2] == ["--worker"]:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        output = worker(sys.argv[2] if len(sys.argv) == 3 else None)
    except ControlError as exc:
        output = unavailable(str(exc))
    except urllib.error.HTTPError as exc:
        output = unavailable("profile_http_rejected", http_status=exc.code)
    except (psutil.AccessDenied, PermissionError):
        output = unavailable("native_control_access_denied")
    except (psutil.NoSuchProcess, ObservationFailure):
        output = unavailable("backend_identity_changed")
    except OSError as exc:
        output = unavailable("native_control_unavailable", error_type=type(exc).__name__,
                             errno=exc.errno, winerror=getattr(exc, "winerror", None))
    except (ValueError, KeyError, TypeError, RecursionError):
        output = unavailable("native_protocol_unavailable")
    print(json.dumps(output, ensure_ascii=False))
    raise SystemExit(0 if output["result"] == "profiles_verified" else 4)
