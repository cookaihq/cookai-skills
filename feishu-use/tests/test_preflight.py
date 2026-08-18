import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import preflight  # noqa: E402


CLI_PATH = "/fake/bin/lark-cli"


def command_result(payload=None, *, stdout=None, stderr="", returncode=0):
    if stdout is None:
        stdout = json.dumps(payload) if payload is not None else ""
    return {
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }


class FakeExecutor:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append((tuple(argv), timeout))
        key = tuple(argv[1:])
        if key not in self.responses:
            raise AssertionError(f"unexpected command: {argv}")
        return self.responses[key]


def base_responses(*, user=None, bot=None, update=None):
    user = user or {
        "status": "ready",
        "available": True,
        "openId": "ou_expected1234",
        "userName": "Alice",
    }
    bot = bot or {"status": "ready", "available": True}
    update = update or {
        "ok": True,
        "action": "up_to_date",
        "current_version": "1.2.3",
        "latest_version": "1.2.3",
    }
    return {
        ("--version",): command_result(stdout="lark-cli version 1.2.3\n"),
        ("update", "--check", "--json"): command_result(update),
        ("auth", "status", "--json"): command_result(
            {
                "appId": "cli_test",
                "brand": "feishu",
                "identities": {"user": user, "bot": bot},
            }
        ),
    }


def run_preflight(responses, **kwargs):
    executor = FakeExecutor(responses)
    report = preflight.run_preflight(
        identity=kwargs.pop("identity", "user"),
        executor=executor,
        cli_locator=lambda _: CLI_PATH,
        **kwargs,
    )
    return report, executor


def test_missing_cli_stops_before_other_checks():
    executor = FakeExecutor({})
    report = preflight.run_preflight(
        identity="user",
        executor=executor,
        cli_locator=lambda _: None,
    )

    assert report["stage"] == "cli_missing"
    assert report["next_action"] == "ask_install"
    assert executor.calls == []


def test_version_can_be_read_from_stderr():
    responses = base_responses()
    responses[("--version",)] = command_result(
        stdout="", stderr="lark-cli version 1.2.3\n"
    )

    report, _ = run_preflight(responses)

    assert report["stage"] == "ready"


def test_update_available_requires_user_choice_before_auth():
    responses = base_responses(
        update={
            "ok": True,
            "action": "update_available",
            "current_version": "1.2.3",
            "latest_version": "1.2.4",
        }
    )
    report, executor = run_preflight(responses)

    assert report["stage"] == "update_available"
    assert report["cli"]["update"]["latest_version"] == "1.2.4"
    assert all("auth" not in call[0] for call in executor.calls)


def test_user_can_explicitly_continue_with_outdated_version():
    responses = base_responses(
        update={
            "ok": True,
            "action": "update_available",
            "current_version": "1.2.3",
            "latest_version": "1.2.4",
        }
    )
    report, _ = run_preflight(
        responses,
        allow_outdated=True,
        expected_open_id="ou_expected1234",
    )

    assert report["stage"] == "ready"
    assert report["cli"]["update"]["status"] == "update_available"


def test_unconfigured_cli_routes_to_app_setup():
    responses = base_responses()
    responses[("auth", "status", "--json")] = command_result(
        {
            "ok": False,
            "error": {
                "type": "configuration",
                "subtype": "not_configured",
                "message": "lark-cli is not configured",
            },
        },
        returncode=3,
    )
    report, _ = run_preflight(responses)

    assert report["stage"] == "config_required"
    assert report["next_action"] == "ask_configure"


def test_missing_named_profile_routes_to_profile_selection():
    responses = base_responses()
    del responses[("auth", "status", "--json")]
    responses[("--profile", "missing", "auth", "status", "--json")] = command_result(
        {
            "ok": False,
            "error": {
                "type": "config",
                "subtype": "not_configured",
                "message": 'profile "missing" not found',
                "hint": "available profiles: default",
            },
        },
        returncode=3,
    )
    report, _ = run_preflight(responses, profile="missing")

    assert report["stage"] == "profile_required"
    assert report["next_action"] == "choose_existing_profile"
    assert report["auth"]["profile"] == "missing"


def test_missing_user_login_is_distinct_from_ready_bot():
    responses = base_responses(
        user={"status": "missing", "available": False},
        bot={"status": "ready", "available": True},
    )
    report, _ = run_preflight(responses)

    assert report["stage"] == "login_required"
    assert report["auth"]["identity"] == "user"


def test_external_provider_missing_user_does_not_start_device_login():
    responses = base_responses(
        user={
            "status": "missing",
            "available": False,
            "message": "User identity: not signed in via credential source openclaw",
            "hint": "managed by the external credential provider and cannot be configured via lark-cli",
        }
    )
    report, _ = run_preflight(responses)

    assert report["stage"] == "identity_unavailable"
    assert report["next_action"] == "repair_external_credential_provider"


def test_not_configured_user_identity_routes_to_policy_check():
    responses = base_responses(
        user={
            "status": "not_configured",
            "available": False,
            "message": "User identity is not available in current credential context",
        }
    )
    report, _ = run_preflight(responses)

    assert report["stage"] == "identity_unavailable"
    assert report["next_action"] == "inspect_identity_policy_or_credentials"


def test_open_id_is_a_strong_account_match():
    report, _ = run_preflight(
        base_responses(), expected_open_id="ou_expected1234"
    )

    assert report["stage"] == "ready"
    assert report["auth"]["account_match"] == "strong"
    assert report["auth"]["user_open_id_masked"] == "ou_expe...1234"


def test_open_id_mismatch_requires_account_replacement_choice():
    report, _ = run_preflight(
        base_responses(), expected_open_id="ou_someone_else"
    )

    assert report["stage"] == "account_mismatch"
    assert report["next_action"] == "ask_replace_account"


def test_name_only_match_requires_confirmation():
    report, _ = run_preflight(base_responses(), expected_name="Alice")

    assert report["stage"] == "account_confirmation_required"
    assert report["auth"]["account_match"] == "weak"

    confirmed, _ = run_preflight(
        base_responses(), expected_name="Alice", accept_name_match=True
    )
    assert confirmed["stage"] == "ready"


def test_missing_scopes_are_reported_after_account_match():
    responses = base_responses()
    responses[("auth", "check", "--scope", "base:record:read wiki:node:retrieve", "--json")] = command_result(
        {
            "ok": False,
            "granted": ["wiki:node:retrieve"],
            "missing": ["base:record:read"],
        },
        returncode=1,
    )
    report, _ = run_preflight(
        responses,
        expected_open_id="ou_expected1234",
        scopes=["base:record:read", "wiki:node:retrieve"],
    )

    assert report["stage"] == "scope_required"
    assert report["auth"]["missing_scopes"] == ["base:record:read"]


def test_external_provider_defers_scope_check_to_provider():
    responses = base_responses(
        user={
            "status": "ready",
            "available": True,
            "openId": "ou_expected1234",
            "userName": "Alice",
            "message": "User identity: ready (provided by openclaw)",
        }
    )
    report, executor = run_preflight(
        responses,
        expected_open_id="ou_expected1234",
        scopes=["base:record:read"],
    )

    assert report["stage"] == "ready"
    assert report["auth"]["scope_check"] == "delegated_to_external_provider"
    assert all("check" not in call[0] for call in executor.calls)


def test_bot_identity_does_not_require_user_login():
    responses = base_responses(user={"status": "missing", "available": False})
    report, _ = run_preflight(responses, identity="bot")

    assert report["stage"] == "ready"
    assert report["auth"]["identity"] == "bot"


def test_legacy_auth_status_can_fall_back_to_auth_list():
    responses = base_responses(
        user={"status": "ready", "available": True, "openId": "", "userName": ""}
    )
    responses[("auth", "list", "--json")] = command_result(
        [
            {
                "appId": "cli_test",
                "tokenStatus": "ready",
                "userName": "Alice",
                "userOpenId": "ou_expected1234",
            }
        ]
    )
    report, _ = run_preflight(
        responses, expected_open_id="ou_expected1234"
    )

    assert report["stage"] == "ready"
    assert report["auth"]["account_match"] == "strong"


def test_update_check_failure_requires_explicit_offline_override():
    responses = base_responses()
    responses[("update", "--check", "--json")] = command_result(
        stderr="network unavailable", returncode=1
    )
    blocked, _ = run_preflight(responses)
    assert blocked["stage"] == "update_check_failed"

    allowed, _ = run_preflight(responses, allow_unknown_version=True)
    assert allowed["stage"] == "ready"
    assert allowed["cli"]["update"]["status"] == "unknown"


def test_update_check_error_payload_is_not_treated_as_current():
    responses = base_responses()
    responses[("update", "--check", "--json")] = command_result(
        {"ok": False, "error": {"type": "network"}}
    )

    report, _ = run_preflight(responses)

    assert report["stage"] == "update_check_failed"


def test_update_check_accepts_current_latest_compatibility_fields():
    responses = base_responses()
    responses[("update", "--check", "--json")] = command_result(
        {"ok": True, "current": "1.2.3", "latest": "1.2.3"}
    )

    report, _ = run_preflight(responses)

    assert report["stage"] == "ready"
    assert report["cli"]["update"]["status"] == "up_to_date"


def test_scope_check_structured_failure_is_not_treated_as_granted():
    responses = base_responses()
    responses[("auth", "check", "--scope", "base:record:read", "--json")] = command_result(
        {"ok": False, "error": {"type": "storage", "message": "keychain unavailable"}},
        returncode=3,
    )

    report, _ = run_preflight(
        responses,
        expected_open_id="ou_expected1234",
        scopes=["base:record:read"],
    )

    assert report["stage"] == "scope_check_failed"


def test_scope_check_lost_token_routes_back_to_login():
    responses = base_responses()
    responses[("auth", "check", "--scope", "base:record:read", "--json")] = command_result(
        {"ok": False, "error": "no_token", "missing": ["base:record:read"]},
        returncode=1,
    )

    report, _ = run_preflight(
        responses,
        expected_open_id="ou_expected1234",
        scopes=["base:record:read"],
    )

    assert report["stage"] == "login_required"


# --- 网络抖动处理（ADR 0006）：瞬时分类与重试 ---


def test_timeout_is_transient():
    result = {
        "returncode": preflight.TIMEOUT_RETURNCODE,
        "stdout": "",
        "stderr": "command timed out",
    }
    assert preflight.is_transient_failure(result) is True


def test_exec_failure_is_not_transient():
    result = {
        "returncode": preflight.EXEC_FAILURE_RETURNCODE,
        "stdout": "",
        "stderr": "[Errno 2] No such file or directory: 'lark-cli'",
    }
    assert preflight.is_transient_failure(result) is False


def test_structured_network_error_is_transient():
    result = command_result({"error": {"type": "network", "message": "reset"}}, returncode=1)
    assert preflight.is_transient_failure(result) is True


def test_structured_auth_error_is_not_transient():
    result = command_result(
        {"error": {"type": "authentication", "code": 401, "message": "token expired"}},
        returncode=1,
    )
    assert preflight.is_transient_failure(result) is False


def test_structured_server_error_is_transient():
    result = command_result({"error": {"type": "server", "code": 503}}, returncode=1)
    assert preflight.is_transient_failure(result) is True


def test_unstructured_network_message_is_transient():
    result = {
        "returncode": 1,
        "stdout": "",
        "stderr": "request failed: getaddrinfo ENOTFOUND open.feishu.cn",
    }
    assert preflight.is_transient_failure(result) is True


def test_unstructured_permission_message_is_not_transient():
    result = {"returncode": 1, "stdout": "", "stderr": "permission denied: missing scope"}
    assert preflight.is_transient_failure(result) is False


def test_execute_command_retries_transient_then_succeeds(monkeypatch):
    calls = []
    sequence = [
        {"returncode": preflight.TIMEOUT_RETURNCODE, "stdout": "", "stderr": "command timed out"},
        {"returncode": 1, "stdout": "", "stderr": "socket hang up"},
        {"returncode": 0, "stdout": "ok", "stderr": ""},
    ]

    def fake_run_once(argv, timeout):
        calls.append(tuple(argv))
        return sequence[len(calls) - 1]

    slept = []
    monkeypatch.setattr(preflight, "run_command_once", fake_run_once)
    monkeypatch.setattr(preflight.time, "sleep", lambda seconds: slept.append(seconds))

    result = preflight.execute_command([CLI_PATH, "auth", "status", "--json"], 20.0)

    assert result["returncode"] == 0
    assert len(calls) == 3
    assert slept == [1, 2]  # 指数退避 1s、2s（ADR 0006 §3）


def test_execute_command_stops_at_max_attempts(monkeypatch):
    calls = []

    def fake_run_once(argv, timeout):
        calls.append(tuple(argv))
        return {
            "returncode": preflight.TIMEOUT_RETURNCODE,
            "stdout": "",
            "stderr": "command timed out",
        }

    monkeypatch.setattr(preflight, "run_command_once", fake_run_once)
    monkeypatch.setattr(preflight.time, "sleep", lambda seconds: None)

    result = preflight.execute_command([CLI_PATH, "--version"], 20.0)

    assert result["returncode"] == preflight.TIMEOUT_RETURNCODE
    assert len(calls) == preflight.NETWORK_MAX_ATTEMPTS


def test_execute_command_does_not_retry_deterministic_failure(monkeypatch):
    calls = []

    def fake_run_once(argv, timeout):
        calls.append(tuple(argv))
        return command_result({"error": {"type": "authentication", "code": 401}}, returncode=1)

    monkeypatch.setattr(preflight, "run_command_once", fake_run_once)
    monkeypatch.setattr(preflight.time, "sleep", lambda seconds: None)

    result = preflight.execute_command([CLI_PATH, "auth", "status", "--json"], 20.0)

    assert result["returncode"] == 1
    assert len(calls) == 1
