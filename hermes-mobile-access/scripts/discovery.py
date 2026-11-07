"""Read-only Desktop backend discovery. No port scan, token read, or process mutation.

The OS observation and protocol probe are separate: a ledger/argv is only a hint.
Only a stable process owning a loopback listener and associated with Desktop can
be selected. Discovery evidence is not permission to publish that listener.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable


PROBE_TIMEOUT_SECONDS = 3.0
DISCOVERY_BUDGET_SECONDS = 20.0
MAX_CANDIDATES = 8
MAX_LEDGER_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    born: float
    executable: str
    desktop: bool = False
    backend_hint: bool = False


@dataclass(frozen=True)
class Listener:
    pid: int
    host: str
    port: int


@dataclass(frozen=True)
class Observation:
    processes: tuple[Process, ...]
    listeners: tuple[Listener, ...]
    # Desktop process ID plus exact peer address/port; IPv4 and IPv6 are distinct.
    desktop_connections: tuple[tuple[int, str, int], ...] = ()
    issues: tuple[str, ...] = ()


class ObservationFailure(Exception):
    pass


def loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def backend_argv(argv: list[str]) -> bool:
    """Recognize CLI entrypoints, not a Python process or arbitrary text containing Hermes."""
    if not argv:
        return False
    if '-m' in argv:
        index = argv.index('-m')
        return (argv[index + 1:index + 2] == ['hermes_cli.main']
                and any(item in ('serve', 'dashboard') for item in argv[index + 2:]))
    for index, value in enumerate(argv[:3]):
        name = value.replace('\\', '/').rsplit('/', 1)[-1].lower()
        if name in ('hermes', 'hermes.exe', 'hermes.cmd', 'hermes.py'):
            return any(item in ('serve', 'dashboard') for item in argv[index + 1:])
    return False


def ledger_hints(path: Path) -> tuple[dict[int, float], list[str]]:
    """Read only the caller-selected ledger. Missing/corrupt metadata is never proof."""
    try:
        if path.stat().st_size > MAX_LEDGER_BYTES:
            return {}, ['ledger_too_large']
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}, []
    except (OSError, UnicodeError, ValueError):
        return {}, ['ledger_unreadable']
    if not isinstance(data, list):
        return {}, ['ledger_invalid']
    hints = {}
    for row in data:
        if not isinstance(row, dict) or row.get('isolated') is True:
            continue
        pid, born = row.get('pid'), row.get('create_time')
        if (type(pid) is int and pid > 0 and type(born) in (int, float)
                and math.isfinite(born) and born > 0
                and row.get('purpose') in ('serve', 'dashboard')):
            hints[pid] = float(born)
    return hints, []


def desktop_owner(process: Process, processes: dict[int, Process]) -> int | None:
    seen = {process.pid}
    pid = process.ppid
    while pid in processes and pid not in seen:
        seen.add(pid)
        ancestor = processes[pid]
        if ancestor.desktop:
            return pid
        pid = ancestor.ppid
    return None


def candidates(observation: Observation) -> list[dict]:
    processes = {p.pid: p for p in observation.processes}
    result = []
    for listener in sorted(set(observation.listeners), key=lambda s: (s.pid, s.host, s.port)):
        process = processes.get(listener.pid)
        if process is None or not 1 <= listener.port <= 65535 or not loopback(listener.host):
            continue
        owner = desktop_owner(process, processes)
        connected = next((pid for pid, host, port in observation.desktop_connections
                          if host == listener.host and port == listener.port
                          and pid in processes and processes[pid].desktop), None)
        # A Desktop child can be a runtime launcher with opaque argv. Its socket
        # must still pass the protocol probe. Unrelated Python listeners are skipped.
        if owner is None and connected is None:
            continue
        if process.desktop or process.born <= 0 or not math.isfinite(process.born):
            continue
        desktop = processes[owner if owner is not None else connected]
        identity = [process.pid, process.born, process.executable, listener.host, listener.port,
                    desktop.pid, desktop.born]
        fingerprint = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
        result.append({'id': fingerprint[:24], 'pid': process.pid, 'born': process.born,
                       'host': listener.host, 'port': listener.port, 'desktop_pid': desktop.pid,
                       'association': 'desktop_ancestry' if owner is not None else 'desktop_connection'})
    return result


def health_summary(body: object) -> dict | None:
    if not isinstance(body, dict):
        return None
    version, commit = body.get('version'), body.get('commit')
    if (body.get('ok') is not True or type(body.get('auth_required')) is not bool
            or not isinstance(version, str)
            or not re.fullmatch(r'[0-9][0-9A-Za-z.+_-]{0,79}', version)
            or not isinstance(commit, str) or not re.fullmatch(r'[0-9a-f]{7,64}', commit)):
        return None
    return {'version': version, 'commit': commit, 'auth_required': body['auth_required']}


def discover(observe: Callable[[], Observation], probe: Callable[[str, int, float], dict],
             *, selected: str | None = None, budget: float = DISCOVERY_BUDGET_SECONDS,
             clock: Callable[[], float] = time.monotonic) -> dict:
    """Reobserve ownership after every HTTP probe; never resurrect a stale candidate."""
    deadline = clock() + budget
    try:
        initial = observe()
    except ObservationFailure as exc:
        return {'state': 'discovery_blocked', 'reason': str(exc), 'selected': None,
                'candidates': [], 'publication': 'not_performed'}
    found = candidates(initial)
    issues = list(initial.issues)
    if len(found) > MAX_CANDIDATES:
        return {'state': 'discovery_blocked', 'reason': 'too_many_candidates',
                'candidates': [], 'issues': issues, 'publication': 'not_performed'}
    results = []
    for candidate in found:
        remaining = deadline - clock()
        if remaining <= 0:
            issues.append('discovery_budget_exhausted')
            break
        response = probe(candidate['host'], candidate['port'], min(PROBE_TIMEOUT_SECONDS, remaining))
        try:
            current = observe()
        except ObservationFailure as exc:
            return {'state': 'discovery_blocked', 'reason': str(exc), 'selected': None,
                    'candidates': [], 'publication': 'not_performed'}
        same = next((item for item in candidates(current) if item['id'] == candidate['id']), None)
        item = dict(candidate, identity='unverified', protocol='not_verified')
        if same is None:
            item['reason'] = 'instance_changed_during_probe'
        else:
            health = health_summary(response.get('body')) if response.get('status') == 200 else None
            if health:
                item.update(identity='verified_os_and_protocol', protocol='hermes_health', **health)
            else:
                item['reason'] = response.get('error', 'unsupported_health_response')
                if response.get('status') in (401, 403):
                    item['reason'] = 'health_requires_authentication'
        results.append(item)
        issues.extend(current.issues)
    verified = [item for item in results if item['identity'] == 'verified_os_and_protocol']
    chosen = None
    if selected:
        chosen = next((item for item in verified if item['id'] == selected), None)
        state = 'backend_discovered' if chosen else 'selection_stale_or_unverified'
    elif len(verified) == 1 and len(found) == 1 and not issues:
        chosen, state = verified[0], 'backend_discovered'
    elif verified:
        state = 'selection_required'
    else:
        state = 'backend_unproven' if found or issues else 'desktop_backend_not_found'
    return {'state': state, 'selected': chosen['id'] if chosen else None,
            'candidates': results, 'issues': sorted(set(issues)),
            'evidence_scope': 'local_os_and_read_only_health', 'publication': 'not_performed',
            'profile': 'not_verified', 'rest_authenticated': 'not_checked',
            'websocket': 'not_checked', 'android': 'not_checked'}


def observe_live(*, ledger: Path | None = None) -> Observation:
    import psutil

    processes, handles, issues = [], {}, []
    hints, ledger_issues = ledger_hints(ledger) if ledger is not None else ({}, [])
    issues.extend(ledger_issues)
    # Avoid create_time/exe/ppid for every system process: on Windows these are
    # much more expensive than the process-name table and may require access.
    names = {p.pid: (p.info.get('name') or '').lower()
             for p in psutil.process_iter(['pid', 'name'], ad_value=None)}
    desktop_ids = {pid for pid, name in names.items() if name in ('hermes', 'hermes.exe')}
    interested = desktop_ids | (set(hints) & set(names))
    for pid in desktop_ids:
        try:
            interested.update(p.pid for p in psutil.Process(pid).children(recursive=True))
        except psutil.AccessDenied:
            issues.append('desktop_children_access_denied')
        except psutil.NoSuchProcess:
            issues.append('desktop_exited')
    # On Windows, per-process net_connections repeats a system-wide table read.
    # Collect it once, then retain only Desktop peers and relevant listeners.
    try:
        socket_table = psutil.net_connections(kind='tcp')
    except psutil.AccessDenied:
        socket_table = None  # macOS commonly requires per-process inspection.
    peer_targets = {(c.raddr.ip, c.raddr.port) for c in socket_table or ()
                    if c.pid in desktop_ids and c.status == psutil.CONN_ESTABLISHED
                    and c.raddr and loopback(c.raddr.ip)}
    connected_owners = {c.pid for c in socket_table or ()
                        if c.status == psutil.CONN_LISTEN
                        and (c.laddr.ip, c.laddr.port) in peer_targets}
    interested.update(connected_owners)
    if socket_table is None:
        # On macOS read only candidate process sockets when the global table
        # requires elevated privileges; denied observation remains unproven.
        interested.update(pid for pid, name in names.items() if name.startswith('python') or 'hermes' in name)
    for pid in sorted(interested - {None}):
        try:
            handle = psutil.Process(pid)
            with handle.oneshot():
                processes.append(Process(pid, handle.ppid(), handle.create_time(), handle.exe(),
                                         desktop=pid in desktop_ids))
            handles[pid] = handle
        except psutil.AccessDenied:
            issues.append('candidate_identity_access_denied')
        except psutil.NoSuchProcess:
            issues.append('candidate_exited')
    index = {p.pid: p for p in processes}
    listeners, desktop_connections, updated = [], [], []
    for proc in processes:
        handle = handles[proc.pid]
        related = (proc.pid in hints or proc.pid in connected_owners or desktop_owner(proc, index) is not None
                   or 'hermes' in proc.executable.lower())
        hint = hints.get(proc.pid) == proc.born
        python = Path(proc.executable).name.lower().startswith('python')
        if (related or python) and not proc.desktop:
            try:
                hint = hint or backend_argv(handle.cmdline())
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                issues.append('candidate_argv_unavailable')
        updated.append(Process(proc.pid, proc.ppid, proc.born, proc.executable, proc.desktop, hint))
        if not (related or hint or proc.desktop):
            continue
        try:
            connections = (handle.net_connections(kind='tcp') if socket_table is None else
                           (c for c in socket_table if c.pid == proc.pid))
            for conn in connections:
                if conn.status == psutil.CONN_LISTEN:
                    listeners.append(Listener(proc.pid, conn.laddr.ip, conn.laddr.port))
                    if (related or hint) and not proc.desktop and not loopback(conn.laddr.ip):
                        issues.append('non_loopback_backend_listener')
                elif proc.desktop and conn.status == psutil.CONN_ESTABLISHED and conn.raddr:
                    if loopback(conn.raddr.ip):
                        desktop_connections.append((proc.pid, conn.raddr.ip, conn.raddr.port))
        except psutil.AccessDenied:
            issues.append('candidate_sockets_access_denied')
        except psutil.NoSuchProcess:
            issues.append('candidate_exited')
    return Observation(tuple(updated), tuple(listeners), tuple(desktop_connections), tuple(issues))


def observe_bounded(timeout: float, *, ledger: Path | None = None) -> Observation:
    if timeout <= 0:
        raise ObservationFailure('discovery_budget_exhausted')
    command = [sys.executable, str(Path(__file__).resolve()), '_observe']
    if ledger is not None:
        command.append(str(ledger))
    try:
        result = subprocess.run(command, capture_output=True, timeout=timeout, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ObservationFailure('os_observation_timeout') from None
    except OSError:
        raise ObservationFailure('os_observation_unavailable') from None
    if result.returncode:
        raise ObservationFailure('os_observation_unavailable')
    try:
        data = json.loads(result.stdout)
        return Observation(tuple(Process(**p) for p in data['processes']),
                           tuple(Listener(**p) for p in data['listeners']),
                           tuple(tuple(c) for c in data['desktop_connections']), tuple(data['issues']))
    except (TypeError, ValueError, KeyError):
        raise ObservationFailure('os_observation_invalid') from None


def discover_live(*, ledger: Path | None = None, selected: str | None = None) -> dict:
    deadline = time.monotonic() + DISCOVERY_BUDGET_SECONDS
    return discover(lambda: observe_bounded(deadline - time.monotonic(), ledger=ledger),
                    probe_live, selected=selected, budget=DISCOVERY_BUDGET_SECONDS)


def probe_live(host: str, port: int, timeout: float) -> dict:
    if not loopback(host) or not 1 <= port <= 65535:
        return {'error': 'non_loopback_probe_refused'}
    # A separate, owned helper enforces a wall-clock budget even if response
    # headers or the body trickle indefinitely. No credentials enter its argv.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '_probe', host, str(port)],
                                capture_output=True, timeout=timeout, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return json.loads(result.stdout) if result.returncode == 0 else {'error': 'probe_failed'}
    except subprocess.TimeoutExpired:
        return {'error': 'probe_timeout'}
    except (OSError, ValueError):
        return {'error': 'probe_failed'}


def _probe_worker(host: str, port: int) -> dict:
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    if not loopback(host) or not 1 <= port <= 65535:
        return {'error': 'non_loopback_probe_refused'}
    authority = f'[{host}]' if ':' in host else host
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(f'http://{authority}:{port}/api/health', timeout=PROBE_TIMEOUT_SECONDS) as response:
            raw = response.read(32769)
            if len(raw) > 32768:
                return {'error': 'health_response_too_large'}
            summary = health_summary(json.loads(raw))
            return {'status': response.status, 'body': dict(ok=True, **summary) if summary else None}
    except urllib.error.HTTPError as exc:
        return {'status': exc.code, 'error': 'health_http_error'}
    except (OSError, ValueError):
        return {'error': 'health_request_failed'}


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '_probe':
        print(json.dumps(_probe_worker(sys.argv[2], int(sys.argv[3]))))
    elif len(sys.argv) in (2, 3) and sys.argv[1] == '_observe':
        from dataclasses import asdict
        print(json.dumps(asdict(observe_live(ledger=Path(sys.argv[2]) if len(sys.argv) == 3 else None))))
