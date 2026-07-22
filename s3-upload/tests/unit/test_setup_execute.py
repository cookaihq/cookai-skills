from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import subprocess

import pytest

from resolver import resolve_target
from setup_adapters import request_digest
from setup_executor import (
    CredentialHandleRegistry, CredentialSink, ExecutionContext, execute_setup_plan,
    guarded_mutation_call,
)
from setup_plan import build_setup_plan
from setup import main as setup_main
from setup_contracts import validate_setup_result
from strict_json import canonicalize
from test_setup_plan import SECRET, planning_context, setup_observation, setup_request


class HappyAdapter:
    synthetic = True

    def __init__(self):
        self.calls = []
        self.observations = [
            setup_observation(state="absent")["observation"],
            setup_observation(state="present")["observation"],
        ]

    def wait_for_login(self, context):
        self.calls.append(("wait-login", None))

    def observe(self, query):
        self.calls.append(("observe", query["phase"]))
        return self.observations.pop(0)

    def guarded_mutate(self, call, credential_sink=None):
        self.calls.append(("guarded-mutate", call["action_id"]))
        return {
            "status": "accepted",
            "created_resource": {
                "resource_type": "bucket",
                "resource_id": "setup-bucket",
            },
            "recovery_instructions": [],
        }


class ScenarioAdapter(HappyAdapter):
    def __init__(self, *, mutation=None, after_hook=None, observations=None):
        super().__init__()
        self.mutation = mutation
        self.after_hook = after_hook
        if observations is not None:
            self.observations = observations

    def observe(self, query):
        value = super().observe(query)
        if query["phase"] == "after" and self.after_hook is not None:
            self.after_hook()
        return value

    def guarded_mutate(self, call, credential_sink=None):
        self.calls.append(("guarded-mutate", call["action_id"]))
        if isinstance(self.mutation, BaseException):
            raise self.mutation
        if self.mutation is not None:
            return copy.deepcopy(self.mutation)
        return {
            "status": "accepted",
            "created_resource": None,
            "recovery_instructions": [],
        }


def confirmation(plan, decision="confirm"):
    return {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-confirmation",
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "decision": decision,
    }


def test_execute_persisted_plan_confirms_actions_and_installs_local_graph(tmp_path):
    planning = planning_context(tmp_path)
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning,
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    adapter = HappyAdapter()

    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            environ={},
            persisted=True,
        ),
    )

    assert exit_code == 0
    assert result["status"] == "completed"
    assert result["action_results"] == [{
        "action_id": "action-1",
        "status": "succeeded",
        "before_digest": plan["actions"][0]["before_digest"],
        "after_digest": result["action_results"][0]["after_digest"],
        "recovery_instructions": [],
    }]
    assert result["action_results"][0]["after_digest"].startswith("sha256:")
    assert result["created_resources"] == [{
        "action_id": "action-1",
        "resource_type": "bucket",
        "resource_id": "setup-bucket",
    }]
    assert result["local_install_result"] == "installed"
    assert adapter.calls == [
        ("wait-login", None),
        ("observe", "before"),
        ("guarded-mutate", "action-1"),
        ("observe", "after"),
    ]
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller=None, use_local_key=False,
    )
    assert resolved.ref.text == "project:setup-target"


@pytest.mark.parametrize(
    ("confirmation_value", "expected_status"),
    [
        ({}, "blocked"),
        ("reject", "blocked"),
        ("mismatch", "plan_stale"),
    ],
)
def test_confirmation_failures_are_structured_and_never_open_adapter(
    tmp_path, confirmation_value, expected_status,
):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    if confirmation_value == "reject":
        confirmation_value = confirmation(plan, "reject")
    elif confirmation_value == "mismatch":
        confirmation_value = confirmation(plan)
        confirmation_value["plan_hash"] = "sha256:" + "0" * 64
    adapter = HappyAdapter()

    result, exit_code = execute_setup_plan(
        plan, confirmation_value, adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 3
    assert result["status"] == expected_status
    assert adapter.calls == []
    assert all(row["status"] == "not_started" for row in result["action_results"])


def test_guarded_mutation_transport_error_is_unknown(tmp_path):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    adapter = ScenarioAdapter(mutation=RuntimeError("connection closed"))

    result, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    assert result["status"] == "unknown"
    assert result["action_results"][0]["status"] == "unknown"
    assert result["local_install_result"] == "not_started"


def test_cloud_success_then_local_snapshot_drift_reports_configuration_changed(tmp_path):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    target_path = Path(
        next(
            row["path"] for row in plan["local_install"]["payload"]["file_snapshots"]
            if row["role"] == "target"
        ),
    )

    def create_competing_target():
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("{}", encoding="utf-8")

    adapter = ScenarioAdapter(after_hook=create_competing_target)
    result, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    assert result["status"] == "partial"
    assert result["action_results"][0]["status"] == "succeeded"
    assert result["local_install_result"] == "configuration_changed"


def test_invalid_resource_type_is_unknown_and_never_emitted(tmp_path):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    adapter = ScenarioAdapter(mutation={
        "status": "accepted",
        "created_resource": {
            "resource_type": "NOT A SETUP IDENTIFIER",
            "resource_id": "setup-bucket",
        },
        "recovery_instructions": [],
    })

    result, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    assert result["status"] == "unknown"
    assert result["created_resources"] == []


def test_execute_cli_with_injected_adapter_emits_one_canonical_result(tmp_path):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    plan_path = tmp_path / "plan.json"
    confirmation_path = tmp_path / "confirmation.json"
    plan_path.write_bytes(canonicalize(plan))
    plan_path.chmod(0o600)
    confirmation_path.write_text(json.dumps(confirmation(plan)), encoding="utf-8")
    stdout = io.BytesIO()
    stderr = io.StringIO()
    adapter = HappyAdapter()

    exit_code = setup_main(
        [
            "execute", "--plan-file", str(plan_path),
            "--confirmation-file", str(confirmation_path),
        ],
        adapter=adapter,
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert not stdout.getvalue().endswith(b"\n")
    result = json.loads(stdout.getvalue())
    assert validate_setup_result(result, plan=plan) == result
    assert result["status"] == "completed"


def test_execute_result_writer_failure_after_mutation_exits_runtime_without_retry(
    tmp_path,
):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    plan_path = tmp_path / "plan.json"
    confirmation_path = tmp_path / "confirmation.json"
    plan_path.write_bytes(canonicalize(plan))
    plan_path.chmod(0o600)
    confirmation_path.write_text(json.dumps(confirmation(plan)), encoding="utf-8")
    adapter = HappyAdapter()

    class FailingWriter:
        def __init__(self):
            self.calls = 0

        def write(self, value):
            self.calls += 1
            raise OSError("synthetic writer failure")

    writer = FailingWriter()
    stderr = io.StringIO()
    exit_code = setup_main(
        [
            "execute", "--plan-file", str(plan_path),
            "--confirmation-file", str(confirmation_path),
        ],
        adapter=adapter,
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=writer,
        stderr=stderr,
    )

    assert exit_code == 1
    assert writer.calls == 1
    assert "result_write_error" in stderr.getvalue()
    assert [call for call in adapter.calls if call[0] == "guarded-mutate"] == [
        ("guarded-mutate", "action-1"),
    ]


def test_execute_cli_enforces_exactly_one_fixture_or_injected_adapter(tmp_path):
    stdout = io.BytesIO()
    stderr = io.StringIO()
    exit_code = setup_main(
        [
            "execute", "--plan-file", str(tmp_path / "missing-plan"),
            "--confirmation-file", str(tmp_path / "missing-confirmation"),
        ],
        adapter=None,
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=stdout,
        stderr=stderr,
    )
    assert exit_code == 2
    assert stdout.getvalue() == b""


def test_execute_shell_consumes_strict_synthetic_fixture(tmp_path):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    action = plan["actions"][0]
    wait_request = {
        "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"],
        "authorization_scope": plan["authorization_scope"],
    }
    before_request = {
        "phase": "before", "plan_id": plan["plan_id"],
        "plan_hash": plan["plan_hash"], "action_id": action["action_id"],
    }
    after_request = {**before_request, "phase": "after"}
    fixture = {
        "schema_version": 1,
        "artifact_type": "s3-upload-setup-fixture",
        "fixture_kind": "synthetic/docs-derived",
        "fixture_id": "generic-happy-v1",
        "provider": "custom",
        "contract_id": "generic.console.v1",
        "surface_version": "synthetic.v1",
        "registry_revision": "generic.v1",
        "official_source_refs": ["https://example.com/synthetic-setup-contract"],
        "redaction_sentinels": [],
        "calls": [
            {
                "operation": "wait-login",
                "request_digest": request_digest(wait_request),
                "response": None,
                "credential_delivery": None,
            },
            {
                "operation": "observe",
                "request_digest": request_digest(before_request),
                "response": setup_observation(state="absent")["observation"],
                "credential_delivery": None,
            },
            {
                "operation": "guarded-mutate",
                "request_digest": request_digest(
                    guarded_mutation_call(plan, action, action["before_digest"]),
                ),
                "response": {
                    "status": "accepted",
                    "created_resource": {
                        "resource_type": "bucket",
                        "resource_id": "setup-bucket",
                    },
                    "recovery_instructions": [],
                },
                "credential_delivery": None,
            },
            {
                "operation": "observe",
                "request_digest": request_digest(after_request),
                "response": setup_observation(state="present")["observation"],
                "credential_delivery": None,
            },
        ],
    }
    plan_path = tmp_path / "plan.json"
    confirmation_path = tmp_path / "confirmation.json"
    fixture_path = tmp_path / "fixture.json"
    plan_path.write_bytes(canonicalize(plan))
    plan_path.chmod(0o600)
    confirmation_path.write_text(json.dumps(confirmation(plan)), encoding="utf-8")
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    script = Path(__file__).parents[2] / "scripts" / "setup.py"

    completed = subprocess.run(
        [
            "python3", str(script), "execute",
            "--plan-file", str(plan_path),
            "--confirmation-file", str(confirmation_path),
            "--fixture", str(fixture_path),
        ],
        cwd=tmp_path,
        env={
            **os.environ,
            "S3_UPLOAD_CONFIG_HOME": str(tmp_path / "home"),
        },
        capture_output=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == b""
    result = json.loads(completed.stdout)
    assert validate_setup_result(result, plan=plan) == result
    assert result["status"] == "completed"


@pytest.mark.parametrize("reflection_location", ["observation", "resource", "recovery"])
def test_adapter_credential_reflection_never_enters_setup_result(
    tmp_path, reflection_location,
):
    plan = build_setup_plan(
        setup_request(), setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    mutation = None
    observations = None
    if reflection_location == "observation":
        reflected = setup_observation(state="absent")["observation"]
        reflected["payload"]["surface_marker"] = SECRET
        observations = [reflected]
    elif reflection_location == "resource":
        mutation = {
            "status": "accepted",
            "created_resource": {
                "resource_type": "bucket", "resource_id": SECRET,
            },
            "recovery_instructions": [],
        }
    else:
        mutation = {
            "status": "definite_failure",
            "created_resource": None,
            "recovery_instructions": ["remove " + SECRET],
        }
    adapter = ScenarioAdapter(mutation=mutation, observations=observations)

    result, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    encoded = canonicalize(result)
    assert SECRET.encode() not in encoded
    if reflection_location == "observation":
        assert result["status"] == "failed"
        assert result["action_results"][0]["before_digest"] is None
    else:
        assert result["status"] == "unknown"


def test_non_synthetic_adapter_requires_every_live_gate_before_access(tmp_path):
    request = setup_request()
    request["proposed_target"]["setup"]["integration_test"] = True
    plan = build_setup_plan(
        request, setup_observation(), context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )
    adapter = HappyAdapter()
    adapter.synthetic = False

    blocked, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={
                "S3_UPLOAD_LIVE_TEST": "1",
                "S3_UPLOAD_LIVE_TEST_TARGET": "project:setup-target",
            },
            persisted=True,
            authorized_action_types=(),
        ),
    )
    assert exit_code == 3
    assert blocked["status"] == "blocked"
    assert adapter.calls == []

    completed, exit_code = execute_setup_plan(
        plan, confirmation(plan), adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={
                "S3_UPLOAD_LIVE_TEST": "1",
                "S3_UPLOAD_LIVE_TEST_TARGET": "project:setup-target",
            },
            persisted=True,
            authorized_action_types=("create-bucket",),
        ),
    )
    assert exit_code == 0
    assert completed["status"] == "completed"


class ContinuousAdapter(HappyAdapter):
    def __init__(self):
        super().__init__()
        self.observations = [
            setup_observation(state="absent")["observation"],
            setup_observation(state="absent")["observation"],
            setup_observation(state="present")["observation"],
        ]


def test_continuous_run_uses_captured_process_credential_and_clears_handle(tmp_path):
    request = setup_request(source="process-memory", persistence="this-run")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    plan_path = tmp_path / "artifacts" / "plan.json"
    plan_path.parent.mkdir()
    original = {
        "access_key_id": "ORIGINALKEY1234",
        "secret_access_key": "original-secret-value",
        "session_token": "",
        "expires_at": None,
    }
    replacement = {
        "access_key_id": "REPLACEDKEY1234",
        "secret_access_key": "replacement-secret-value",
        "session_token": "",
        "expires_at": None,
    }
    environ = {
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON": json.dumps(
            {"setup-key": original}, separators=(",", ":"),
        ),
    }
    registry = CredentialHandleRegistry()
    adapter = ContinuousAdapter()
    stdout = io.BytesIO()
    stderr = io.StringIO()

    def confirm_and_replace_environment(plan):
        environ["S3_UPLOAD_PROJECT_CREDENTIALS_JSON"] = json.dumps(
            {"setup-key": replacement}, separators=(",", ":"),
        )
        return confirmation(plan)

    exit_code = setup_main(
        [
            "run", "--request-file", str(request_path),
            "--plan-out", str(plan_path),
        ],
        adapter=adapter,
        environ=environ,
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=stdout,
        stderr=stderr,
        confirmation_callback=confirm_and_replace_environment,
        handle_registry=registry,
    )

    assert exit_code == 0
    result = json.loads(stdout.getvalue())
    assert result["status"] == "completed"
    assert adapter.calls == [
        ("wait-login", None),
        ("observe", "initial"),
        ("observe", "before"),
        ("guarded-mutate", "action-1"),
        ("observe", "after"),
    ]
    assert registry.live_count == 0
    assert plan_path.exists()
    persisted_plan = plan_path.read_bytes()
    assert original["secret_access_key"].encode() not in persisted_plan
    assert replacement["secret_access_key"].encode() not in persisted_plan
    assert original["secret_access_key"].encode() not in stdout.getvalue()
    assert replacement["secret_access_key"].encode() not in stdout.getvalue()


def test_continuous_rejection_publishes_plan_but_never_mutates_and_clears_handle(tmp_path):
    request = setup_request(source="process-memory", persistence="this-run")
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    plan_path = tmp_path / "artifacts" / "plan.json"
    plan_path.parent.mkdir()
    environ = {
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON": json.dumps(
            {"setup-key": {
                "access_key_id": "ORIGINALKEY1234",
                "secret_access_key": "original-secret-value",
                "session_token": "",
                "expires_at": None,
            }},
            separators=(",", ":"),
        ),
    }
    registry = CredentialHandleRegistry()
    adapter = ContinuousAdapter()
    stdout = io.BytesIO()

    exit_code = setup_main(
        [
            "run", "--request-file", str(request_path),
            "--plan-out", str(plan_path),
        ],
        adapter=adapter,
        environ=environ,
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=stdout,
        stderr=io.StringIO(),
        confirmation_callback=lambda plan: confirmation(plan, "reject"),
        handle_registry=registry,
    )

    assert exit_code == 3
    assert json.loads(stdout.getvalue())["status"] == "blocked"
    assert adapter.calls == [("wait-login", None), ("observe", "initial")]
    assert registry.live_count == 0
    assert plan_path.exists()


def test_credential_handle_is_plan_bound_and_one_shot():
    registry = CredentialHandleRegistry()
    profile = {
        "access_key_id": "ONESHOTKEY1234",
        "secret_access_key": "one-shot-secret",
        "session_token": "",
        "expires_at": None,
    }
    handle = registry.capture(profile)
    registry.bind(
        handle,
        "123e4567-e89b-42d3-a456-426614174000",
        "sha256:" + "1" * 64,
    )
    assert registry.consume(
        handle,
        "123e4567-e89b-42d3-a456-426614174000",
        "sha256:" + "1" * 64,
    ) == profile
    with pytest.raises(ValueError):
        registry.consume(
            handle,
            "123e4567-e89b-42d3-a456-426614174000",
            "sha256:" + "1" * 64,
        )
    assert registry.live_count == 0


@pytest.mark.parametrize("persisted", [True, False])
def test_process_memory_plan_replay_without_live_binding_is_pre_adapter_stale(
    tmp_path, persisted,
):
    request = setup_request(source="process-memory", persistence="this-run")
    profile = {
        "access_key_id": "REPLAYKEY1234",
        "secret_access_key": "replay-secret-value",
        "session_token": "",
        "expires_at": None,
    }
    environment = {
        "S3_UPLOAD_PROJECT_CREDENTIALS_JSON": json.dumps(
            {"setup-key": profile}, separators=(",", ":"),
        ),
    }
    plan = build_setup_plan(
        request,
        setup_observation(),
        context=planning_context(tmp_path, environ=environment),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
        credential_handle_id="handle-replay",
        credential_override=profile,
    )
    adapter = HappyAdapter()

    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            environ=environment,
            persisted=persisted,
        ),
        handle_registry=None,
    )

    assert exit_code == 3
    assert result["status"] == "plan_stale"
    assert adapter.calls == []


class IssuanceAdapter:
    synthetic = True

    def __init__(self):
        self.calls = []
        self.observations = [
            setup_observation(state="absent")["observation"],
            setup_observation(state="present")["observation"],
            setup_observation(state="absent")["observation"],
            setup_observation(state="present")["observation"],
        ]
        self.sink = None

    def wait_for_login(self, context):
        self.calls.append(("wait-login", None))

    def observe(self, query):
        self.calls.append(("observe", query["action_id"], query["phase"]))
        return self.observations.pop(0)

    def guarded_mutate(self, call, credential_sink=None):
        self.calls.append(("guarded-mutate", call["action_id"]))
        if call["action_type"] == "issue-long-lived-access-key":
            assert credential_sink is not None
            self.sink = credential_sink
            credential_sink.deliver({
                "access_key_id": "ISSUEDKEY1234",
                "secret_access_key": "issued-secret-value",
            })
            resource = {
                "resource_type": "access-key-resource",
                "resource_id": "credential-resource-1",
            }
        else:
            assert credential_sink is None
            resource = {
                "resource_type": "bucket",
                "resource_id": "setup-bucket",
            }
        return {
            "status": "accepted",
            "created_resource": resource,
            "recovery_instructions": [],
        }


def planned_issuance_plan(tmp_path):
    request = setup_request(
        source="planned-issuance",
        persistence="project",
        actions=["create-bucket", "issue-long-lived-access-key"],
    )
    request["credential_ref"] = "project:issued-key"
    request["proposed_target"]["credential"] = "project:issued-key"
    return build_setup_plan(
        request,
        setup_observation(),
        context=planning_context(tmp_path),
        plan_id_factory=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )


def test_planned_issuance_delivers_only_through_sink_and_installs_complete_profile(
    tmp_path,
):
    plan = planned_issuance_plan(tmp_path)
    adapter = IssuanceAdapter()

    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path),
            config_home=str(tmp_path / "home"),
            environ={},
            persisted=True,
        ),
    )

    assert exit_code == 0
    assert result["status"] == "completed"
    assert [row["status"] for row in result["action_results"]] == [
        "succeeded", "succeeded",
    ]
    assert result["local_install_result"] == "installed"
    assert b"ISSUEDKEY1234" not in canonicalize(result)
    assert b"issued-secret-value" not in canonicalize(result)
    assert adapter.calls == [
        ("wait-login", None),
        ("observe", "action-1", "before"),
        ("guarded-mutate", "action-1"),
        ("observe", "action-1", "after"),
        ("observe", "action-2", "before"),
        ("guarded-mutate", "action-2"),
        ("observe", "action-2", "after"),
    ]
    assert adapter.sink.live is False
    resolved = resolve_target(
        cwd=str(tmp_path), config_home=str(tmp_path / "home"), environ={},
        cli_target=None, cli_caller=None, use_local_key=False,
    )
    assert resolved.ref.text == "project:setup-target"
    assert resolved.credential.access_key_id == "ISSUEDKEY1234"
    assert resolved.credential.session_token == ""
    assert resolved.credential.expires_at is None


class IssuanceFailureAdapter(IssuanceAdapter):
    def __init__(self, behavior):
        super().__init__()
        self.behavior = behavior

    def guarded_mutate(self, call, credential_sink=None):
        if call["action_type"] != "issue-long-lived-access-key":
            return super().guarded_mutate(call, credential_sink)
        self.calls.append(("guarded-mutate", call["action_id"]))
        self.sink = credential_sink
        if self.behavior == "definite":
            return {
                "status": "definite_failure",
                "created_resource": None,
                "recovery_instructions": [],
            }
        delivery = {
            "access_key_id": "ISSUEDKEY1234",
            "secret_access_key": "issued-secret-value",
        }
        if self.behavior == "sts":
            credential_sink.deliver({
                **delivery,
                "session_token": "forbidden-session-token",
            })
        credential_sink.deliver(delivery)
        if self.behavior == "duplicate":
            credential_sink.deliver(delivery)
        if self.behavior == "unknown":
            return {
                "status": "unknown",
                "created_resource": {
                    "resource_type": "access-key-resource",
                    "resource_id": "credential-resource-unknown",
                },
                "recovery_instructions": [],
            }
        resource_id = (
            "ISSUEDKEY1234"
            if self.behavior == "reflected-resource"
            else "credential-resource-1"
        )
        return {
            "status": "accepted",
            "created_resource": {
                "resource_type": "access-key-resource",
                "resource_id": resource_id,
            },
            "recovery_instructions": [],
        }


@pytest.mark.parametrize(
    ("behavior", "expected_status", "expected_action_status"),
    [
        ("definite", "partial", "definite_failure"),
        ("unknown", "unknown", "unknown"),
        ("sts", "unknown", "unknown"),
        ("duplicate", "unknown", "unknown"),
        ("reflected-resource", "unknown", "unknown"),
    ],
)
def test_issuance_failure_paths_require_manual_revoke_without_secret_leakage(
    tmp_path, behavior, expected_status, expected_action_status,
):
    plan = planned_issuance_plan(tmp_path)
    adapter = IssuanceFailureAdapter(behavior)

    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    assert result["status"] == expected_status
    assert result["action_results"][1]["status"] == expected_action_status
    assert "manual_revoke_and_reissue" in result["recovery_instructions"]
    encoded = canonicalize(result)
    assert b"issued-secret-value" not in encoded
    assert b"ISSUEDKEY1234" not in encoded
    if behavior in {"unknown", "duplicate", "reflected-resource"}:
        assert b"ISSU****1234" in encoded
    assert adapter.sink.live is False
    if behavior == "reflected-resource":
        assert all(
            row["resource_id"] != "ISSUEDKEY1234"
            for row in result["created_resources"]
        )


def test_final_local_drift_stops_before_credential_mutation(tmp_path):
    plan = planned_issuance_plan(tmp_path)

    class DriftAdapter(IssuanceAdapter):
        def observe(self, query):
            value = super().observe(query)
            if query["action_id"] == "action-2" and query["phase"] == "before":
                env_file = tmp_path / ".env.local"
                env_file.write_text(
                    env_file.read_text(encoding="utf-8") + "# concurrent change\n",
                    encoding="utf-8",
                )
            return value

    adapter = DriftAdapter()
    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
        ),
    )

    assert exit_code == 1
    assert result["status"] == "partial"
    assert result["local_install_result"] == "configuration_changed"
    assert [row["status"] for row in result["action_results"]] == [
        "succeeded", "not_started",
    ]
    assert ("guarded-mutate", "action-2") not in adapter.calls
    assert adapter.sink is None


def test_local_failure_after_issuance_requires_manual_revoke_and_clears_sink(tmp_path):
    plan = planned_issuance_plan(tmp_path)
    adapter = IssuanceAdapter()

    def fail_before_credential(boundary):
        if boundary == "before-credential":
            raise RuntimeError("synthetic local write failure")

    result, exit_code = execute_setup_plan(
        plan,
        confirmation(plan),
        adapter=adapter,
        context=ExecutionContext(
            project_root=str(tmp_path), config_home=str(tmp_path / "home"),
            environ={}, persisted=True,
            install_fault=fail_before_credential,
        ),
    )

    assert exit_code == 1
    assert result["status"] == "partial"
    assert result["action_results"][1]["status"] == "succeeded"
    assert result["local_install_result"] == "failed"
    assert "manual_revoke_and_reissue" in result["recovery_instructions"]
    assert adapter.sink.live is False
    assert b"issued-secret-value" not in canonicalize(result)


def test_credential_sink_is_bound_one_shot_and_non_revealing():
    sink = CredentialSink(
        "123e4567-e89b-42d3-a456-426614174000",
        "sha256:" + "1" * 64,
        "action-1",
    )
    sink.deliver({
        "access_key_id": "SINKTESTKEY1",
        "secret_access_key": "sink-test-secret",
    })
    assert "SINKTESTKEY1" not in repr(sink)
    assert sink._consume((
        "123e4567-e89b-42d3-a456-426614174000",
        "sha256:" + "1" * 64,
        "action-1",
    )) == {
        "access_key_id": "SINKTESTKEY1",
        "secret_access_key": "sink-test-secret",
        "session_token": "",
        "expires_at": None,
    }
    with pytest.raises(ValueError):
        sink.deliver({
            "access_key_id": "SINKTESTKEY2",
            "secret_access_key": "another-secret",
        })


def test_continuous_run_uses_the_same_planned_issuance_sink(tmp_path):
    request = setup_request(
        source="planned-issuance",
        persistence="project",
        actions=["create-bucket", "issue-long-lived-access-key"],
    )
    request["credential_ref"] = "project:issued-key"
    request["proposed_target"]["credential"] = "project:issued-key"
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    plan_path = tmp_path / "artifacts" / "plan.json"
    plan_path.parent.mkdir()

    class ContinuousIssuanceAdapter(IssuanceAdapter):
        def __init__(self):
            super().__init__()
            self.observations.insert(
                0, setup_observation(state="absent")["observation"],
            )

        def observe(self, query):
            if query["phase"] == "initial":
                self.calls.append(("observe", "initial"))
                return self.observations.pop(0)
            return super().observe(query)

    adapter = ContinuousIssuanceAdapter()
    registry = CredentialHandleRegistry()
    stdout = io.BytesIO()
    exit_code = setup_main(
        [
            "run", "--request-file", str(request_path),
            "--plan-out", str(plan_path),
        ],
        adapter=adapter,
        environ={},
        cwd=str(tmp_path),
        config_home=str(tmp_path / "home"),
        stdout=stdout,
        stderr=io.StringIO(),
        confirmation_callback=lambda plan: confirmation(plan),
        handle_registry=registry,
    )

    assert exit_code == 0
    assert json.loads(stdout.getvalue())["status"] == "completed"
    assert registry.live_count == 0
    assert adapter.sink.live is False
    assert b"issued-secret-value" not in plan_path.read_bytes()
    assert b"ISSUEDKEY1234" not in plan_path.read_bytes()
