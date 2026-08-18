#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import select
import sys

# 同目录模块入 sys.path：直接执行本文件时 Python 会自动加入 scripts/，被当模块
# import（如 tests/）时靠 conftest 注入；这里显式加一次，两种入口都成立。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

if __name__ == "__main__":
    # 运行时 bootstrap（ADR 0007 §1.4）：不在 <skill>/.venv 就 exec 拉回去，
    # venv 缺失按 uv.lock 自动重建。必须先于下面的业务模块 import。
    # 提示：本文件与 setuptools 的 setup.py 无关——scripts/ 是普通脚本目录、不是
    # 包，pyproject 里没有 [build-system] 且 [tool.uv] package = false，uv/pip
    # 不会把它当构建脚本执行。
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

from setup_adapters import (
    FixtureAdapter, GENERIC_FIXTURE_EXTENSION,
)
from setup_contracts import (
    validate_setup_plan, validate_setup_request, validate_setup_result,
)
from setup_executor import (
    CredentialHandleRegistry, ExecutionContext, execute_setup_plan,
)
from setup_plan import (
    PlanningContext, SetupPlanError, build_setup_plan, capture_process_credential,
    preflight_plan_sink, publish_setup_plan, read_setup_input,
)
from strict_json import StrictJSONError, canonicalize, loads


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Maintainer setup orchestration for s3-upload")
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--request-file", required=True)
    plan.add_argument("--observation-file", required=True)
    plan.add_argument("--plan-out", required=True)
    plan.add_argument("--use-local-key", action="store_true")
    execute = commands.add_parser("execute")
    execute.add_argument("--plan-file", required=True)
    execute.add_argument("--confirmation-file", required=True)
    execute.add_argument("--fixture")
    execute.add_argument("--use-local-key", action="store_true")
    run = commands.add_parser("run")
    run.add_argument("--request-file", required=True)
    run.add_argument("--plan-out", required=True)
    run.add_argument("--fixture")
    run.add_argument("--confirmation-timeout-seconds", type=int, default=900)
    run.add_argument("--use-local-key", action="store_true")
    return root


def _write_bytes(stream, value: bytes) -> None:
    try:
        written = stream.write(value)
        expected = len(value)
    except TypeError:
        text = value.decode("utf-8")
        written = stream.write(text)
        expected = len(text)
    if isinstance(written, int) and written != expected:
        raise OSError("short setup artifact write")
    if hasattr(stream, "flush"):
        stream.flush()


def _read_run_confirmation(
    *, plan, stdin, callback, timeout_seconds: int,
):
    if callback is not None:
        try:
            return callback(plan)
        except (EOFError, TimeoutError):
            return {
                "schema_version": 1,
                "artifact_type": "s3-upload-setup-confirmation",
                "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"],
                "decision": "reject",
            }
    if stdin is None:
        stdin = sys.stdin
    try:
        descriptor = stdin.fileno()
    except (AttributeError, OSError):
        descriptor = None
    if descriptor is not None:
        readable, _, _ = select.select([descriptor], [], [], timeout_seconds)
        if not readable:
            return {
                "schema_version": 1,
                "artifact_type": "s3-upload-setup-confirmation",
                "plan_id": plan["plan_id"],
                "plan_hash": plan["plan_hash"],
                "decision": "reject",
            }
    try:
        text = stdin.readline(1048578)
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        if not text or len(text.encode("utf-8")) > 1048576:
            raise StrictJSONError("confirmation is missing or too large")
        return loads(text)
    except Exception:
        return {}


def _request_live_gate_satisfied(request, adapter, environ, authorized_actions) -> bool:
    if getattr(adapter, "synthetic", False):
        return True
    return (
        environ.get("S3_UPLOAD_LIVE_TEST") == "1"
        and environ.get("S3_UPLOAD_LIVE_TEST_TARGET") == request["target_ref"]
        and request["proposed_target"]["setup"]["integration_test"] is True
        and all(
            action in authorized_actions
            for action in request["requested_action_types"]
        )
    )


def _fixture_adapter(value):
    provider = value.get("provider") if isinstance(value, dict) else None
    if provider == GENERIC_FIXTURE_EXTENSION.provider:
        extension = GENERIC_FIXTURE_EXTENSION
    else:
        try:
            from provider_setup_candidates import fixture_extension_for
            extension = fixture_extension_for(
                value.get("provider"),
                value.get("contract_id"),
                value.get("surface_version"),
                value.get("registry_revision"),
            )
        except (ImportError, AttributeError):
            extension = None
        if extension is None:
            raise SetupPlanError("fixture setup registry identity is unavailable")
    return FixtureAdapter(value, extension=extension)


def _adapter_extension(adapter, provider):
    extension = getattr(adapter, "extension", None)
    if extension is None and provider == GENERIC_FIXTURE_EXTENSION.provider:
        extension = GENERIC_FIXTURE_EXTENSION
    if extension is None or extension.provider != provider:
        raise SetupPlanError("adapter setup registry identity is unavailable")
    return extension


def main(
    argv=None, *, adapter=None, environ=None, cwd=None, config_home=None,
    stdin=None, stdout=None, stderr=None, confirmation_callback=None,
    authorized_action_types=(),
    handle_registry=None,
) -> int:
    environ = dict(os.environ) if environ is None else environ
    cwd = os.path.abspath(cwd or os.getcwd())
    config_home = config_home or environ.get(
        "S3_UPLOAD_CONFIG_HOME", os.path.expanduser("~/.config/s3-upload"),
    )
    stdout = stdout or sys.stdout.buffer
    stderr = stderr or sys.stderr
    try:
        args = parser().parse_args(argv)
        if args.command == "plan":
            request = read_setup_input(args.request_file)
            observation = read_setup_input(args.observation_file)
            if request.identity == observation.identity:
                raise SetupPlanError("request and observation inputs must be distinct")
            context = PlanningContext(
                project_root=cwd,
                config_home=config_home,
                environ=environ,
                use_local_key=args.use_local_key,
            )
            sink = preflight_plan_sink(
                args.plan_out, context=context, inputs=(request, observation),
            )
            plan = build_setup_plan(request.value, observation.value, context=context)
            encoded = publish_setup_plan(sink, plan)
            try:
                _write_bytes(stdout, encoded)
            except Exception:
                print("[s3-upload setup] output_write_error", file=stderr)
                return 1
            return 0
        if args.command == "execute":
            if (adapter is None) == (args.fixture is None):
                raise SetupPlanError("execute requires exactly one adapter or fixture")
            plan_input = read_setup_input(args.plan_file)
            plan = validate_setup_plan(plan_input.value)
            active_adapter = adapter
            fixture_input = None
            if args.fixture is not None:
                fixture_input = read_setup_input(args.fixture)
                active_adapter = _fixture_adapter(fixture_input.value)
                if fixture_input.identity == plan_input.identity:
                    raise SetupPlanError("fixture and plan inputs must be distinct")
                active_adapter.validate_execution_shape(plan, login_done=False)
            try:
                confirmation_input = read_setup_input(args.confirmation_file)
                if confirmation_input.identity == plan_input.identity or (
                    fixture_input is not None
                    and confirmation_input.identity == fixture_input.identity
                ):
                    confirmation_value = {}
                else:
                    confirmation_value = confirmation_input.value
            except Exception:
                confirmation_value = {}
            result, exit_code = execute_setup_plan(
                plan,
                confirmation_value,
                adapter=active_adapter,
                context=ExecutionContext(
                    project_root=cwd,
                    config_home=config_home,
                    environ=environ,
                    persisted=True,
                    use_local_key=args.use_local_key,
                    authorized_action_types=tuple(authorized_action_types),
                ),
            )
            try:
                _write_bytes(
                    stdout,
                    canonicalize(validate_setup_result(result, plan=plan)),
                )
            except Exception:
                print("[s3-upload setup] result_write_error", file=stderr)
                return 1
            return exit_code
        if args.command == "run":
            if not 1 <= args.confirmation_timeout_seconds <= 86400:
                raise SetupPlanError("confirmation timeout must be between 1 and 86400")
            if (adapter is None) == (args.fixture is None):
                raise SetupPlanError("run requires exactly one adapter or fixture")
            request_input = read_setup_input(args.request_file)
            request = validate_setup_request(request_input.value)
            active_adapter = adapter
            fixture_input = None
            if args.fixture is not None:
                fixture_input = read_setup_input(args.fixture)
                active_adapter = _fixture_adapter(fixture_input.value)
                if fixture_input.identity == request_input.identity:
                    raise SetupPlanError("fixture and request inputs must be distinct")
            planning_context = PlanningContext(
                project_root=cwd,
                config_home=config_home,
                environ=environ,
                use_local_key=args.use_local_key,
            )
            sink = preflight_plan_sink(
                args.plan_out,
                context=planning_context,
                inputs=tuple(
                    item for item in (request_input, fixture_input) if item is not None
                ),
            )
            if not _request_live_gate_satisfied(
                request, active_adapter, environ, tuple(authorized_action_types),
            ):
                raise SetupPlanError("continuous setup live gate is unavailable")
            registry = handle_registry or CredentialHandleRegistry()
            try:
                try:
                    extension = _adapter_extension(
                        active_adapter, request["provider"],
                    )
                    login_result = active_adapter.wait_for_login({
                        "mode": "run",
                        "provider": request["provider"],
                        "target_ref": request["target_ref"],
                        "credential_ref": request["credential_ref"],
                    })
                    if login_result is not None:
                        raise SetupPlanError(
                            "adapter login seam returned unexpected data",
                        )
                    initial = active_adapter.observe({
                        "phase": "initial",
                        "provider": request["provider"],
                        "target_ref": request["target_ref"],
                        "credential_ref": request["credential_ref"],
                    })
                    observation = {
                        "schema_version": 1,
                        "artifact_type": "s3-upload-setup-observation",
                        "provider": request["provider"],
                        "contract_id": extension.contract_id,
                        "surface_version": extension.surface_version,
                        "registry_revision": extension.registry_revision,
                        "observation": initial,
                    }
                    handle_id = None
                    captured = None
                    if request["credential_source_category"] == "process-memory":
                        captured = capture_process_credential(request, planning_context)
                        handle_id = registry.capture(captured)
                    plan = build_setup_plan(
                        request,
                        observation,
                        context=planning_context,
                        credential_handle_id=handle_id,
                        credential_override=captured,
                    )
                    if isinstance(active_adapter, FixtureAdapter):
                        active_adapter.validate_execution_shape(
                            plan, login_done=True,
                        )
                    if handle_id is not None:
                        registry.bind(handle_id, plan["plan_id"], plan["plan_hash"])
                    publish_setup_plan(sink, plan)
                except Exception:
                    return 1
                print(
                    "[s3-upload setup] confirm"
                    f" plan={sink.path} id={plan['plan_id']} hash={plan['plan_hash']}",
                    file=stderr,
                )
                confirmation_value = _read_run_confirmation(
                    plan=plan,
                    stdin=stdin,
                    callback=confirmation_callback,
                    timeout_seconds=args.confirmation_timeout_seconds,
                )
                result, exit_code = execute_setup_plan(
                    plan,
                    confirmation_value,
                    adapter=active_adapter,
                    context=ExecutionContext(
                        project_root=cwd,
                        config_home=config_home,
                        environ=environ,
                        persisted=False,
                        use_local_key=args.use_local_key,
                        authorized_action_types=tuple(authorized_action_types),
                    ),
                    login_done=True,
                    handle_registry=registry,
                )
                try:
                    _write_bytes(
                        stdout,
                        canonicalize(validate_setup_result(result, plan=plan)),
                    )
                except Exception:
                    print("[s3-upload setup] result_write_error", file=stderr)
                    return 1
                return exit_code
            finally:
                registry.clear()
        raise SetupPlanError("unsupported setup command")
    except SystemExit:
        raise
    except Exception:
        print("[s3-upload setup] configuration_error", file=stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
