import copy
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.error

import psutil
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import desktop_profiles as profiles
import native_control
from native_control import ControlError


@pytest.fixture
def protocol(tmp_path):
    home = str(tmp_path.resolve())
    other = str((tmp_path / "profiles/work").resolve())
    candidate = {"id": "a" * 24, "pid": 42, "born": 100, "host": "127.0.0.1", "port": 54499,
                 "version": "0.22.5", "commit": "b" * 40}
    identity = {"protocol": 1, "runtime_protocol": 1, "pid": 42, "kind": "hermes-gateway",
                "api_origin": "http://127.0.0.1:54499", "code_sha": "b" * 40,
                "hermes_home": home, "state": "ready", "instance_id": "fixture_instance",
                "served_profiles": [{"profile_id": path, "home": path} for path in (home, other)]}
    roster = {"profiles": [{"name": "default", "path": home, "is_default": True, "secret": "drop-this"},
                            {"name": "work", "path": other, "is_default": False}]}
    calls, used = [], set()

    def control(home_arg, candidate_arg, verb="identify", params=None):
        assert home_arg == home and candidate_arg == candidate
        calls.append((verb, params))
        if verb == "identify":
            return copy.deepcopy(identity)
        assert params == {"profile_id": home, "instance_id": "fixture_instance", "purpose": "native-http"}
        return {"ticket": "secret_" + str(len(calls)), "instance_id": "fixture_instance",
                "profile_id": home, "runtime_protocol": 1}

    def get(origin, path, ticket):
        assert origin == "http://127.0.0.1:54499" and ticket not in used
        used.add(ticket)
        if path == "/api/profiles":
            return copy.deepcopy(roster)
        assert path == "/api/profiles/active"
        return {"active": "default", "current": "default", "secret": "drop-this"}

    return home, candidate, identity, roster, calls, used, control, get


def test_native_profile_reads_are_scoped_single_use_and_redacted(protocol):
    home, candidate, identity, roster, calls, used, control, get = protocol
    result = profiles.inspect_profiles(candidate, home, control=control, get=get, stable=lambda: True)
    assert result["result"] == "profiles_verified"
    assert result["home"] == home
    assert [row["name"] for row in result["profiles"]] == ["default", "work"]
    assert all(row["served"] for row in result["profiles"])
    assert len(used) == 2
    assert [verb for verb, _ in calls] == ["identify", "session-ticket", "session-ticket", "identify"]
    assert result["rest_authenticated"] == "profiles_only"
    assert result["publication"] == "not_performed" and result["android"] == "not_checked"
    assert "secret_" not in json.dumps(result) and "drop-this" not in json.dumps(result)


@pytest.mark.parametrize("field,value", [
    ("pid", 43), ("api_origin", "http://attacker.test"), ("code_sha", "c" * 40),
    ("state", "draining"), ("runtime_protocol", 2), ("protocol", True),
    ("kind", "unknown"), ("instance_id", ""), ("served_profiles", [])])
def test_identity_mismatch_precedes_credential_issue(protocol, field, value):
    home, candidate, identity, _, calls, _, control, get = protocol
    identity[field] = value
    with pytest.raises(ControlError):
        profiles.inspect_profiles(candidate, home, control=control, get=get, stable=lambda: True)
    assert [verb for verb, _ in calls] == ["identify"]


def test_stale_candidate_makes_no_requests(protocol):
    home, candidate, _, _, calls, _, control, get = protocol
    with pytest.raises(ControlError, match="backend_identity_changed"):
        profiles.inspect_profiles(candidate, home, control=control, get=get, stable=lambda: False)
    assert not calls


@pytest.mark.parametrize('present,issues,allowed', [
    (True, ('candidate_exited',), True),
    (False, ('candidate_exited',), False),
    (True, ('candidate_identity_access_denied',), False),
])
def test_worker_distinguishes_unrelated_exit_from_target_loss(protocol, monkeypatch, present, issues, allowed):
    from discovery import Observation, Process, Listener, candidates
    home, candidate, _, _, calls, _, control, get = protocol
    initial = Observation((Process(1, 0, 50, 'desktop', desktop=True), Process(42, 1, 100, 'gateway')),
                          (Listener(42, '127.0.0.1', 54499),))
    candidate['id'] = candidates(initial)[0]['id']
    observed = Observation(initial.processes, initial.listeners if present else (), issues=issues)
    monkeypatch.setattr(profiles, 'discover_live', lambda **kw: {'selected':candidate['id'], 'candidates':[candidate]})
    monkeypatch.setattr(profiles, 'process_home', lambda _:home)
    monkeypatch.setattr(profiles, 'observe_bounded', lambda _:observed)
    inspect = profiles.inspect_profiles
    monkeypatch.setattr(profiles, 'inspect_profiles', lambda c, h, **kw:inspect(c,h,control=control,get=get,**kw))
    if allowed:
        assert profiles.worker()['result'] == 'profiles_verified'
    else:
        with pytest.raises(ControlError, match='backend_identity_changed'):
            profiles.worker()
        assert not calls


def test_changed_gateway_after_reads_discards_result(protocol):
    home, candidate, identity, _, _, _, control, get = protocol
    def changed_get(origin, path, ticket):
        result = get(origin, path, ticket)
        if path.endswith("active"):
            identity["instance_id"] = "next_instance"
        return result
    with pytest.raises(ControlError, match="backend_identity_changed"):
        profiles.inspect_profiles(candidate, home, control=control, get=changed_get, stable=lambda: True)


def test_mismatched_ticket_never_sent_to_http(protocol):
    home, candidate, _, _, _, used, control, get = protocol
    def wrong_ticket(*args, **kwargs):
        result = control(*args, **kwargs)
        if "ticket" in result:
            result["profile_id"] = "other"
        return result
    with pytest.raises(ControlError, match="native_ticket_mismatch"):
        profiles.inspect_profiles(candidate, home, control=wrong_ticket, get=get, stable=lambda: True)
    assert not used


def test_roster_only_profile_is_not_marked_served(protocol, tmp_path):
    home, candidate, _, roster, _, _, control, get = protocol
    roster["profiles"].append({"name": "offline", "path": str(tmp_path / "offline"), "is_default": False})
    result = profiles.inspect_profiles(candidate, home, control=control, get=get, stable=lambda: True)
    assert result["profiles"][-1]["served"] is False


@pytest.mark.parametrize("change", ["duplicate_name", "duplicate_path", "relative_path", "missing_served"])
def test_ambiguous_or_changed_roster_rejected(protocol, change):
    home, candidate, _, roster, _, _, control, get = protocol
    if change == "duplicate_name":
        roster["profiles"][1]["name"] = "default"
    elif change == "duplicate_path":
        roster["profiles"][1]["path"] = home
    elif change == "relative_path":
        roster["profiles"][1]["path"] = "../other"
    else:
        roster["profiles"].pop()
    with pytest.raises(ControlError):
        profiles.inspect_profiles(candidate, home, control=control, get=get, stable=lambda: True)


def test_worker_timeout_and_invalid_selection_are_explicit(monkeypatch):
    calls = []
    def timeout(*args, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired("worker", 35, output=b"secret-value")
    monkeypatch.setattr(profiles.subprocess, "run", timeout)
    assert profiles.read_profiles(selected="invalid")["reason"] == "invalid_candidate"
    assert not calls
    result = profiles.read_profiles()
    assert result["reason"] == "profile_query_timeout"
    assert "secret-value" not in json.dumps(result)
    assert calls[0]["timeout"] == 35


def test_control_interface_has_no_mutating_lifecycle_verbs():
    with pytest.raises(ControlError, match="control_verb_not_allowed"):
        native_control.query("unused", {}, "stop")


@pytest.fixture
def raw_http_peer():
    """A real accepted socket records whether any request bytes reached it."""
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(1)
    sock.settimeout(5)
    state = {'port': sock.getsockname()[1], 'received': b'', 'status': 200, 'errors': []}

    def serve():
        try:
            with sock.accept()[0] as conn:
                conn.settimeout(5)
                while b'\r\n\r\n' not in state['received']:
                    data = conn.recv(4096)
                    if not data:
                        return
                    state['received'] += data
                body = b'{"profiles":[]}'
                conn.sendall(('HTTP/1.1 %s Test\r\n' % state['status']).encode()
                    + b'Content-Type: application/json\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\nLocation: http://127.0.0.1:1/\r\nConnection: close\r\n\r\n'
                    + body)
        except Exception as exc:
            state['errors'].append(type(exc).__name__)
        finally:
            sock.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    yield state
    thread.join(timeout=6)
    sock.close()
    assert not thread.is_alive()
    assert not state['errors']


def tcp_candidate(process, port):
    return {'pid': process.pid, 'born': process.create_time(), 'host': '127.0.0.1', 'port': port}


def test_profile_http_authenticates_only_after_accepted_socket_owner_verified(raw_http_peer):
    peer = raw_http_peer
    candidate = tcp_candidate(psutil.Process(), peer['port'])
    result = profiles.http_get(f'http://127.0.0.1:{peer["port"]}', '/api/profiles',
                              'synthetic_single_use_ticket', candidate=candidate)
    assert result == {'profiles': []}
    assert peer['received'].count(b'X-Hermes-Gateway-Ticket: synthetic_single_use_ticket') == 1


def test_replaced_profile_listener_receives_no_ticket_or_request_bytes(raw_http_peer):
    # The expected backend remains alive under the same user. Another process
    # owns the listener now, so PID existence and a successful connect are insufficient.
    child = subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(10)'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        peer = raw_http_peer
        candidate = tcp_candidate(psutil.Process(child.pid), peer['port'])
        with pytest.raises(ControlError, match='upstream_peer_owner_unverified'):
            profiles.http_get(f'http://127.0.0.1:{peer["port"]}', '/api/profiles',
                              'synthetic_single_use_ticket', candidate=candidate)
        assert peer['received'] == b''
    finally:
        if child.poll() is None:
            child.terminate()
        child.wait(timeout=5)


def test_profile_redirect_is_rejected_without_reusing_native_ticket(raw_http_peer):
    peer = raw_http_peer
    peer['status'] = 302
    candidate = tcp_candidate(psutil.Process(), peer['port'])
    with pytest.raises(urllib.error.HTTPError) as exc:
        profiles.http_get(f'http://127.0.0.1:{peer["port"]}', '/api/profiles',
                          'synthetic_single_use_ticket', candidate=candidate)
    assert exc.value.code == 302
    assert peer['received'].count(b'X-Hermes-Gateway-Ticket:') == 1


@pytest.mark.parametrize('born,owner,reason', [
    (100, 'other-user', 'backend_user_mismatch'),
    (101, 'same-user', 'backend_identity_changed')])
def test_account_mismatch_is_distinct_from_process_replacement(monkeypatch, born, owner, reason):
    class Process:
        def __init__(self, pid=None):
            self.pid = pid
        def create_time(self):
            return born
        def username(self):
            return owner if self.pid else 'same-user'
    monkeypatch.setattr(native_control.psutil, 'Process', Process)
    with pytest.raises(ControlError, match=reason):
        native_control.check_process({'pid': 42, 'born': 100})
    output = profiles.unavailable(reason)
    assert output['detail']
    assert owner not in json.dumps(output)
