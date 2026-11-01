"""Native Windows ownership, job lifetime and private ACLs (stdlib only).

Process handles bind inspection and termination to one process incarnation.
The runner owns a kill-on-close Job before it starts frpc, so neither a crashed
runner nor an interrupted launch can leave an untracked client behind.
"""
import contextlib
import ctypes
from ctypes import wintypes as w
import functools
import os
import subprocess

if os.name != "nt":
    raise ImportError("windows_runtime is Windows-only")

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi = ctypes.WinDLL("advapi32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")


def _api(dll, name, args, result=w.BOOL):
    function = getattr(dll, name)
    function.argtypes, function.restype = args, result
    return function


OpenProcess = _api(k32, "OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE)
CloseHandle = _api(k32, "CloseHandle", [w.HANDLE])
WaitForSingleObject = _api(k32, "WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD)
GetCurrentProcess = _api(k32, "GetCurrentProcess", [], w.HANDLE)
GetProcessTimes = _api(k32, "GetProcessTimes", [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4)
QueryFullProcessImageName = _api(k32, "QueryFullProcessImageNameW",
                               [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)])
TerminateProcess = _api(k32, "TerminateProcess", [w.HANDLE, w.UINT])
NtQueryInformationProcess = _api(ntdll, "NtQueryInformationProcess",
    [w.HANDLE, w.ULONG, w.LPVOID, w.ULONG, ctypes.POINTER(w.ULONG)], w.LONG)
CreateJobObject = _api(k32, "CreateJobObjectW", [w.LPVOID, w.LPCWSTR], w.HANDLE)
SetInformationJobObject = _api(k32, "SetInformationJobObject", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD])
AssignProcessToJobObject = _api(k32, "AssignProcessToJobObject", [w.HANDLE, w.HANDLE])
OpenProcessToken = _api(advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)])
GetTokenInformation = _api(advapi, "GetTokenInformation", [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)])
ConvertSidToStringSid = _api(advapi, "ConvertSidToStringSidW", [w.LPVOID, ctypes.POINTER(w.LPWSTR)])
ConvertStringSecurityDescriptor = _api(advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)])
SetFileSecurity = _api(advapi, "SetFileSecurityW", [w.LPCWSTR, w.DWORD, w.LPVOID])
LocalFree = _api(k32, "LocalFree", [w.LPVOID], w.LPVOID)


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return ok


@contextlib.contextmanager
def process_handle(pid, terminate=False):
    # SYNCHRONIZE | QUERY_LIMITED_INFORMATION; command-line query also needs
    # QUERY_INFORMATION on supported Windows 10/11 systems.
    access = 0x100000 | 0x1000 | 0x0400 | (0x0001 if terminate else 0)
    handle = OpenProcess(access, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == 87:  # ERROR_INVALID_PARAMETER: no such PID
            yield None
            return
        raise ctypes.WinError(error)
    try:
        yield handle
    finally:
        CloseHandle(handle)


class UnicodeString(ctypes.Structure):
    _fields_ = [("Length", w.USHORT), ("MaximumLength", w.USHORT), ("Buffer", w.LPVOID)]


def _snapshot(handle, command=True):
    if WaitForSingleObject(handle, 0) != 258:  # WAIT_TIMEOUT = still active
        return {}
    times = [w.FILETIME() for _ in range(4)]
    _check(GetProcessTimes(handle, *(ctypes.byref(t) for t in times)))
    size = w.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    _check(QueryFullProcessImageName(handle, 0, image, ctypes.byref(size)))
    result = {"process_start": "win:%d" % ((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
              "exe": image.value}
    if command:
        needed = w.ULONG()
        NtQueryInformationProcess(handle, 60, None, 0, ctypes.byref(needed))
        if not needed.value or needed.value > 1024 * 1024:
            raise OSError("Windows 无法读取完整进程参数；未信任该 PID。")
        buffer = ctypes.create_string_buffer(needed.value)
        status = NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(needed))
        if status < 0:
            raise OSError("Windows 进程参数查询失败 (NTSTATUS 0x%08x)。" % (status & 0xffffffff))
        value = UnicodeString.from_buffer(buffer)
        result["command"] = ctypes.wstring_at(value.Buffer, value.Length // 2)
    return result


def snapshot(pid, command=True):
    with process_handle(pid) as handle:
        return _snapshot(handle, command) if handle else {}


def alive(pid):
    with process_handle(pid) as handle:
        return bool(handle) and WaitForSingleObject(handle, 0) == 258


def matches(record, current):
    args = record.get("identity_args")
    return bool(current and args and record.get("process_start")
                and record["process_start"] == current["process_start"]
                and os.path.normcase(record.get("exe", "")) == os.path.normcase(current["exe"])
                and subprocess.list2cmdline(args) == current.get("command"))


def terminate(record):
    with process_handle(record["pid"], terminate=True) as handle:
        if not handle or not _snapshot(handle, False):
            return
        if not matches(record, _snapshot(handle)):
            raise OSError("进程身份已变化，未终止进程。")
        _check(TerminateProcess(handle, 1))
        if WaitForSingleObject(handle, 10000) != 0:
            raise OSError("等待 Windows 进程终止超时；保留身份记录。")


class BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", w.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", w.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", w.DWORD), ("SchedulingClass", w.DWORD)]


class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount",
        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def own_job():
    """Keep the returned non-inheritable handle for the runner's whole life."""
    job = _check(CreateJobObject(None, None))
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        _check(SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        _check(AssignProcessToJobObject(job, GetCurrentProcess()))
        return job
    except BaseException:
        CloseHandle(job)
        raise


@functools.lru_cache(maxsize=1)
def user_sid():
    token = w.HANDLE()
    _check(OpenProcessToken(GetCurrentProcess(), 8, ctypes.byref(token)))
    try:
        needed = w.DWORD()
        GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        _check(GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)))
        sid = ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0]
        value = w.LPWSTR()
        _check(ConvertSidToStringSid(sid, ctypes.byref(value)))
        try:
            return value.value
        finally:
            LocalFree(ctypes.cast(value, w.LPVOID))
    finally:
        CloseHandle(token)


def private_acl(path):
    """Protected DACL: this user, SYSTEM and Administrators only."""
    inherit = "OICI" if path.is_dir() else ""
    sddl = "D:P" + "".join("(A;%s;FA;;;%s)" % (inherit, sid) for sid in (user_sid(), "SY", "BA"))
    descriptor = w.LPVOID()
    _check(ConvertStringSecurityDescriptor(sddl, 1, ctypes.byref(descriptor), None))
    try:
        _check(SetFileSecurity(str(path), 4 | 0x80000000, descriptor))
    finally:
        LocalFree(descriptor)
