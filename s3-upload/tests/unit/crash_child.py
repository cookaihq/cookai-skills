import json
import os
import signal
import sys


def counting_transport(counter_path, kill_after_count):
    def transport(method, url, headers, body):
        from s3 import Response

        descriptor = os.open(counter_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(descriptor, b"1")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if kill_after_count:
            os.kill(os.getpid(), signal.SIGKILL)
        return Response(200)

    return transport


def main():
    payload = json.loads(sys.argv[1])
    sys.path.insert(0, payload["scripts"])
    from delivery_workflow import acknowledge, publish
    from plan_store import PlanStore
    from resolver import resolve_target

    boundary = payload["boundary"]
    store = PlanStore(payload["state_root"])

    def hook(name):
        if name == boundary:
            os.kill(os.getpid(), signal.SIGKILL)

    resolved = resolve_target(
        cwd=payload["cwd"], config_home=payload["config_home"], environ={},
        cli_target=None, cli_caller=payload["caller"], use_local_key=False,
    )
    if payload["phase"] == "publish":
        publish(
            resolved=resolved, store=store, token=payload["token"],
            transport=counting_transport(payload["counter"],
                                         boundary == "in_transport"),
            project_root=payload["cwd"], config_home=payload["config_home"],
            caller=payload["caller"], executable_path=payload["executable_path"],
            cwd=payload["cwd"], on_boundary=hook,
        )
        os._exit(0)
    with open(payload["result_out"], encoding="utf-8") as handle:
        result_text = handle.read()
    acknowledge(
        store=store, token=payload["token"], caller=payload["caller"],
        executable_path=payload["executable_path"], cwd=payload["cwd"],
        result_text=result_text, ack_out=payload["ack_out"],
        project_root=payload["cwd"], config_home=payload["config_home"],
        on_boundary=hook,
    )
    os._exit(0)


if __name__ == "__main__":
    main()
