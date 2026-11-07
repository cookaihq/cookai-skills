"""Hermes protocol-v1 local control transport; no lifecycle or arbitrary verbs.

Windows uses the existing user's named pipe; macOS uses the existing private
Unix socket. Caller runs this module inside the bounded profile query worker.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys
import time

import psutil

MAX_RESPONSE_BYTES = 512 * 1024
CONTROL_TIMEOUT = 3.0


class ControlError(Exception):
    """Only fixed local reason codes may leave the worker."""


def check_process(candidate):
    process = psutil.Process(candidate["pid"])
    if process.create_time() != candidate["born"]:
        raise ControlError("backend_identity_changed")
    if process.username() != psutil.Process().username():
        raise ControlError("backend_user_mismatch")
    return process


def verify_connected_peer(candidate, local, peer):
    """Check the accepted TCP tuple before transmitting a ticket or user data."""
    def address(value):
        return ipaddress.ip_address(value[0]), int(value[1])

    process = check_process(candidate)
    if address(peer) != (ipaddress.ip_address(candidate['host']), candidate['port']):
        raise ControlError('upstream_peer_mismatch')
    matches = [row for row in process.net_connections(kind='tcp')
               if row.laddr and row.raddr and address(row.laddr) == address(peer)
               and address(row.raddr) == address(local) and row.status == psutil.CONN_ESTABLISHED]
    check_process(candidate)
    if not matches:
        raise ControlError('upstream_peer_owner_unverified')


def process_home(candidate):
    process = check_process(candidate)
    # Only this named field is retained; no credentials or unrelated environment
    # values leave the function or get passed to another process.
    home = process.environ().get("HERMES_HOME")
    check_process(candidate)
    if not home or not Path(home).is_absolute():
        raise ControlError("hermes_home_unavailable")
    return str(Path(home).resolve())


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return remaining


def _windows_exchange(home, candidate, request, deadline):
    import ctypes
    from ctypes import wintypes
    import _winapi as win

    key = hashlib.sha256(os.path.normcase(str(Path(home).resolve())).encode()).hexdigest()[:16]
    pipe = rf"\\.\pipe\hermes-gateway-{key}"
    while True:
        _remaining(deadline)
        try:
            # Identification only: the server can verify, never impersonate us.
            handle = win.CreateFile(pipe, 0xC0000000, 0, win.NULL, win.OPEN_EXISTING,
                                    win.FILE_FLAG_OVERLAPPED | 0x00110000, win.NULL)
            break
        except OSError as exc:
            if exc.winerror != 231:
                raise
            win.WaitNamedPipe(pipe, max(1, int(_remaining(deadline) * 1000)))

    def complete(operation):
        try:
            if win.WaitForSingleObject(operation.event, max(1, int(_remaining(deadline) * 1000))) != win.WAIT_OBJECT_0:
                raise TimeoutError()
            count, error = operation.GetOverlappedResult(False)
            if error:
                raise OSError(error, "native_io_failed")
            return count
        except BaseException:
            operation.cancel()
            operation.GetOverlappedResult(True)
            raise

    try:
        api = ctypes.WinDLL("kernel32", use_last_error=True).GetNamedPipeServerProcessId
        api.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
        api.restype = wintypes.BOOL
        owner = wintypes.ULONG()
        if not api(handle, ctypes.byref(owner)) or owner.value != candidate["pid"]:
            raise ControlError("control_owner_mismatch")
        check_process(candidate)
        while request:
            operation, _ = win.WriteFile(handle, request, overlapped=True)
            sent = complete(operation)
            if not sent:
                raise ConnectionError()
            request = request[sent:]
        chunks = bytearray()
        while b"\n" not in chunks:
            operation, _ = win.ReadFile(handle, min(4096, MAX_RESPONSE_BYTES + 1 - len(chunks)), overlapped=True)
            count = complete(operation)
            if not count:
                raise ConnectionError()
            chunks.extend(operation.getbuffer()[:count])
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ControlError("control_response_too_large")
        return bytes(chunks).split(b"\n", 1)[0]
    finally:
        win.CloseHandle(handle)


def _private(path, kind):
    info = path.lstat()
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ControlError("control_path_not_private")


def _unix_socket_path(home):
    home = Path(home)
    _private(home, stat.S_ISDIR)
    direct = home / "gateway.sock"
    if not direct.exists():
        pointer = home / "gateway.sock.path"
        _private(pointer, stat.S_ISREG)
        with pointer.open("rb") as stream:
            data = stream.read(4097)
        if len(data) > 4096:
            raise ControlError("control_pointer_invalid")
        direct = Path(data.decode("utf-8-sig").strip())
        if not direct.is_absolute():
            raise ControlError("control_pointer_invalid")
    _private(direct.parent, stat.S_ISDIR)
    _private(direct, stat.S_ISSOCK)
    return direct


def _macos_exchange(home, candidate, request, deadline):
    path = _unix_socket_path(home)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(_remaining(deadline))
        sock.connect(str(path))
        # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2. The kernel reports the
        # owner, rather than trusting the peer's JSON to name its own PID.
        peer_pid = struct.unpack("i", sock.getsockopt(0, 2, struct.calcsize("i")))[0]
        if peer_pid != candidate["pid"]:
            raise ControlError("control_owner_mismatch")
        check_process(candidate)
        sock.settimeout(_remaining(deadline))
        sock.sendall(request)
        chunks = bytearray()
        while b"\n" not in chunks:
            sock.settimeout(_remaining(deadline))
            chunk = sock.recv(min(4096, MAX_RESPONSE_BYTES + 1 - len(chunks)))
            if not chunk:
                raise ConnectionError()
            chunks.extend(chunk)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise ControlError("control_response_too_large")
        return bytes(chunks).split(b"\n", 1)[0]


def query(home, candidate, verb="identify", params=None):
    if verb not in ("identify", "session-ticket"):
        raise ControlError("control_verb_not_allowed")
    request = {"protocol": 1, "id": 1, "verb": verb}
    if params is not None:
        request["params"] = params
    raw = json.dumps(request).encode() + b"\n"
    deadline = time.monotonic() + CONTROL_TIMEOUT
    if sys.platform == "win32":
        response = _windows_exchange(home, candidate, raw, deadline)
    elif sys.platform == "darwin":
        response = _macos_exchange(home, candidate, raw, deadline)
    else:
        raise ControlError("supported_desktop_os_required")
    data = json.loads(response)
    if (not isinstance(data, dict) or data.get("ok") is not True or type(data.get("protocol")) is not int
            or data["protocol"] != 1 or data.get("id") != 1 or not isinstance(data.get("result"), dict)):
        raise ControlError("control_response_rejected")
    return data["result"]
