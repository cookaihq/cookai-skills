"""Internal detached log filter, launched only with the skill's sys.executable.

The supervisor and frpc share a private process group. The owning lifecycle
command checks process birth, argv and project ID before signalling that group.
"""
import argparse
import json
import os
import signal
import subprocess
import sys


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
    with open(args.log, "a", encoding="utf-8", buffering=1) as log:
        child = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, encoding="utf-8", errors="replace")
        def terminate(_signal, _frame):
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass  # The group signal may already have stopped frpc.
        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        for line in child.stdout:
            for secret in secrets:
                if secret:
                    replacement = secret[:4] + "****" + secret[-4:] if len(secret) > 8 else "****"
                    line = line.replace(secret, replacement)
            log.write(line)
        return child.wait()


if __name__ == "__main__":
    sys.exit(main())
