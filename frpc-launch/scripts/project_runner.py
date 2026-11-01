"""Detached client owner and log filter; all secrets stay in memory.

POSIX uses a private process group; Windows uses a kill-on-close Job Object.
The caller commits the identity record before releasing the stdin launch gate.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from runtime_files import protect

LOG_LIMIT = 5 * 1024 * 1024


class PrivateLog:
    def __init__(self, path, secrets):
        self.path, self.secrets = Path(path), secrets
        self.stream = None
        self.size = self.path.stat().st_size if self.path.exists() else 0

    def write(self, line):
        for secret in self.secrets:
            if secret:
                line = line.replace(secret, secret[:4] + "****" + secret[-4:] if len(secret) > 8 else "****")
        if self.stream is None:
            self.stream = self.path.open("a", encoding="utf-8", buffering=1)
            protect(self.path)
        if self.size >= LOG_LIMIT:
            self.stream.close()
            try:
                os.replace(self.path, self.path.with_suffix(self.path.suffix + ".1"))
            except PermissionError:
                # A concurrent Windows reader may briefly hold a non-delete-
                # sharing handle. Keep the client alive and retry next line.
                self.stream = self.path.open("a", encoding="utf-8", buffering=1)
            else:
                self.stream = self.path.open("w", encoding="utf-8", buffering=1)
                protect(self.path)
                self.size = 0
        self.stream.write(line)
        self.size += len(line.encode("utf-8"))

    def close(self):
        if self.stream:
            self.stream.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", required=True)
    parser.add_argument("--mode", choices=("official", "sakura"), required=True)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--config")
    args = parser.parse_args()
    secrets = sorted(set(json.loads(os.environ.pop("FRPC_LAUNCH_RUNTIME_SECRETS", "[]"))), key=len, reverse=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("FRPC_LAUNCH_") or k == "FRPC_LAUNCH_RUNTIME_CONFIG"}
    argv = [args.binary] + (["-c", args.config] if args.mode == "official" else [])
    log = PrivateLog(args.log, secrets)
    child = None
    try:
        if os.name == "nt":
            from windows_runtime import own_job
            job = own_job()  # held until process exit; closing it terminates this process too
        if sys.stdin.buffer.read(3) != b"GO\n":
            return 1  # caller died before recording ownership: no client was started
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace",
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        def terminate(_signal, _frame):
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass  # The group signal may already have stopped frpc.
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        for line in child.stdout:
            log.write(line)
        code = child.wait()
        log.write("[runner] client exited (code=%s)\n" % code)
        return code
    except Exception as exc:
        log.write("[runner] startup/runtime error: %s\n" % exc)
        return 1
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        log.close()


if __name__ == "__main__":
    sys.exit(main())
