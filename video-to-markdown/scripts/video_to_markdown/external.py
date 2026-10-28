"""The sole external dependency execution boundary. No shell interpolation."""
from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    missing: bool = False

    def record(self) -> dict:
        return asdict(self)


def json_output(text: str) -> Any:
    """CLI JSON may follow diagnostic lines. Never print the raw diagnostics."""
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in "[{":
            try:
                value, end = decoder.raw_decode(text[index:])
                if not text[index + end:].strip():
                    return value
            except ValueError:
                pass
    return {}


def write_once(task_dir: Path, private: dict, key: str, argv: list[str], *, cwd: Path, timeout: float) -> Result:
    """An intent without a receipt is uncertain. Never repeat that write."""
    from . import state
    operations = private.setdefault("operations", {})
    receipt = task_dir / ".state" / ("receipt-" + key + ".json")
    if key in operations:
        if receipt.exists():
            return Result(**state.read_json(receipt))
        return Result(-1, timed_out=True)
    operations[key] = {"intent_at": state.utc_now(), "argv": argv}
    state.save_private(task_dir, private)
    result = execute(argv, cwd=cwd, timeout=timeout)
    state.atomic_write_json(receipt, result.record())
    return result


def execute(argv: list[str], *, cwd: Path, timeout: float) -> Result:
    if timeout <= 0:
        return Result(-1, timed_out=True)
    try:
        if os.name == "nt":
            return _windows(argv, cwd, timeout)
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            getattr(os, "killpg")(proc.pid, getattr(signal, "SIGKILL"))
            out, err = proc.communicate()
        return Result(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), timed_out)
    except FileNotFoundError:
        return Result(-1, missing=True)
    except OSError:
        return Result(-1)  # Process-launch failure is not proof of a remote write outcome.


def _windows(argv: list[str], cwd: Path, timeout: float) -> Result:
    # Assign a suspended process to a Job before it can create children. Closing the
    # Job kills the entire dependency tree (PowerShell -> Node included).
    import pywintypes
    import win32api
    import win32con
    import win32event
    import win32file
    import win32job
    import win32pipe
    import win32process
    from concurrent.futures import ThreadPoolExecutor

    handles = []
    job = None
    process = None
    try:
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.bInheritHandle = True
        out_r, out_w = win32pipe.CreatePipe(security, 0)
        err_r, err_w = win32pipe.CreatePipe(security, 0)
        handles.extend([out_r, out_w, err_r, err_w])
        for reader in (out_r, err_r):
            win32api.SetHandleInformation(reader, win32con.HANDLE_FLAG_INHERIT, 0)
        null = win32file.CreateFile("NUL", win32con.GENERIC_READ, win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                                    security, win32con.OPEN_EXISTING, 0, None)
        handles.append(null)
        startup = win32process.STARTUPINFO()
        startup.dwFlags = win32con.STARTF_USESTDHANDLES
        startup.hStdInput, startup.hStdOutput, startup.hStdError = null, out_w, err_w
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        process, thread, _, _ = win32process.CreateProcess(None, subprocess.list2cmdline(argv), None, None, True,
            win32con.CREATE_SUSPENDED | win32con.CREATE_NO_WINDOW, None, str(cwd), startup)
        handles.extend([process, thread])
        win32job.AssignProcessToJobObject(job, process)
        win32process.ResumeThread(thread)
        out_w.Close()
        err_w.Close()

        def read(handle):
            chunks = []
            while True:
                try:
                    _, block = win32file.ReadFile(handle, 65536)
                    if not block:
                        break
                    chunks.append(block)
                except pywintypes.error as exc:
                    if exc.winerror in (109, 232):
                        break
                    raise
            return b"".join(chunks).decode("utf-8", "replace")

        with ThreadPoolExecutor(max_workers=2) as executor:
            out, err = executor.submit(read, out_r), executor.submit(read, err_r)
            timed_out = win32event.WaitForSingleObject(process, max(1, int(timeout * 1000))) == win32con.WAIT_TIMEOUT
            if timed_out:
                win32job.TerminateJobObject(job, 1)
                win32event.WaitForSingleObject(process, 5000)
            code = win32process.GetExitCodeProcess(process)
            job.Close()
            job = None
            return Result(code, out.result(), err.result(), timed_out)
    except pywintypes.error as exc:
        if process is not None:
            win32process.TerminateProcess(process, 1)
        return Result(-1, missing=exc.winerror in (2, 3))
    finally:
        if job is not None:
            job.Close()
        for handle in reversed(handles):
            handle.Close()
