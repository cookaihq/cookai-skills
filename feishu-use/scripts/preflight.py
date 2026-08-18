#!/usr/bin/env python3
"""Read-only lark-cli readiness, version, identity, account, and scope checks.

调用一律用 `uv run --project <skill 目录> <skill 目录>/scripts/preflight.py ...`，
禁止用系统解释器直接起本文件。真起错了也有兜底：下面的 bootstrap（ADR 0007 §1.4）
会把进程 exec 回 `<skill>/.venv` 的解释器，环境缺失时先按 `uv.lock` 自动重建。

**bootstrap 段只用 Python 3.9 兼容语法、只用 stdlib**：它可能先被系统 python3
（macOS 自带 3.9.6）执行，用了新语法会在 SyntaxError 阶段就死掉，兜底反成故障点。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

# --------------------------------------------------------------------------
# 运行时 bootstrap（ADR 0007 §1.4）
# --------------------------------------------------------------------------
# 一次性再入护栏：exec 之后仍不在目标 venv，说明 venv 目录本身坏了。没有这个标记
# 会无限 execv 且零输出。标记值存的是**本轮的目标 venv realpath**，不是布尔——
# 变量被外部环境 export 时值不匹配，就不算本轮的再入，仍照常自动重建。
REEXEC_ENV = "FEISHU_USE_BOOTSTRAP_REEXEC"

UV_INSTALL_HINT = "curl -LsSf https://astral.sh/uv/install.sh | sh"

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPTS_DIR)

# 重定位只认 uv 原生的 UV_PROJECT_ENVIRONMENT，且**基准必须是项目根**——uv 0.8 把
# 相对值按项目根解析，按 CWD 解析会在用户自己的项目目录里设了相对值时把进程 exec
# 进用户项目的 venv。绝对值不受影响：os.path.join 遇到绝对路径直接返回它。
VENV_DIR = os.path.join(SKILL_DIR, os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")
VENV_PY = os.path.join(VENV_DIR, "bin", "python")


def _bootstrap_fail(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(1)


def _manual_rebuild_hint():
    # 必须 shell 引用：路径含空格时，未引用的 `rm -rf /tmp/sp ace/.venv` 被照抄
    # 执行会删掉两个无关路径。
    return "rm -rf %s && uv sync --project %s --no-dev" % (
        shlex.quote(VENV_DIR), shlex.quote(SKILL_DIR)
    )


def _venv_is_valid():
    """有效 venv = 解释器在 + pyvenv.cfg 在。

    只判 bin/python 存在是不够的：sync 中断、手工建的同名目录、残留软链都会让解释
    器存在而目录不是 venv，此时跳过修复直接 execv 会陷入无限重启。
    """
    return (os.path.exists(VENV_PY)
            and os.path.exists(os.path.join(VENV_DIR, "pyvenv.cfg")))


def _require_uv():
    """uv 是系统级程序，缺失/版本过低只报错给命令，不擅自安装（ADR 0007 §4.2）。"""
    try:
        probe = subprocess.run(["uv", "--version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        _bootstrap_fail("uv 未安装。请执行：" + UV_INSTALL_HINT)
    parts = probe.stdout.decode("utf-8", "replace").split()  # "uv 0.8.11 (...)"
    found = parts[1] if len(parts) > 1 else "0"
    try:
        numeric = tuple(int(x) for x in (found.split(".") + ["0", "0"])[:2])
    except ValueError:
        # uv 出错时 stdout 是 "error: ..." 之类，硬 int() 会抛未捕获的 ValueError；
        # 按「版本不可用」处理，仍走可复制命令的报错。
        numeric = (0, 0)
    if numeric < (0, 8):
        _bootstrap_fail("uv 版本过低（需 >= 0.8，当前 %s）。请执行：uv self update"
                        % found)


def _sync_runtime():
    """按 uv.lock 冻结重建 skill 自有环境（ADR 0007 §4.1：自动修复）。"""
    sys.stderr.write("[bootstrap] 运行环境缺失，正在按 uv.lock 重建 %s ...\n" % VENV_DIR)
    try:
        # `--no-dev`：这里重建的是**运行时**环境。不加的话 uv 会把 dev 组
        # （pytest 等）一并装进 .venv，让运行时环境带上只有跑测试才需要的包。
        # 跑测试时显式用 `uv sync --project <dir> --group dev`。
        sync = subprocess.run(["uv", "sync", "--project", SKILL_DIR, "--no-dev"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=600)  # 网络调用必设总预算（ADR 0006 §5）
    except subprocess.TimeoutExpired:
        _bootstrap_fail("uv sync 超过 600 秒未完成，疑似网络异常。请手工执行："
                        + _manual_rebuild_hint())
    if sync.returncode != 0 or not _venv_is_valid():
        _bootstrap_fail("uv sync 失败，无法重建运行环境（请手工执行：%s）：\n%s"
                        % (_manual_rebuild_hint(),
                           sync.stdout.decode("utf-8", "replace")))


def _ensure_runtime():
    """确保当前进程运行在目标 venv 里；不是则 exec 拉回去，缺失则先重建。"""
    target = os.path.realpath(VENV_DIR)
    if os.path.realpath(sys.prefix) == target:
        # 已在目标 venv：清掉本轮标记，避免派生的子进程继承后误判。
        if os.environ.get(REEXEC_ENV) == target:
            os.environ.pop(REEXEC_ENV, None)
        return
    if os.environ.get(REEXEC_ENV) == target:
        # 只认「值等于本轮目标」才算再入：外部 export 了同名变量、或嵌套调用的是
        # 另一个目标时，这里不成立，仍照常走下面的自动重建。
        _bootstrap_fail("运行环境异常：已重启到 %s 但解释器仍不在该 venv 内，"
                        "目录疑似损坏。\n请手工重建：%s"
                        % (VENV_DIR, _manual_rebuild_hint()))
    if not _venv_is_valid():
        _require_uv()
        _sync_runtime()
    os.environ[REEXEC_ENV] = target  # putenv，execv 后的进程能读到
    os.execv(VENV_PY, [VENV_PY] + sys.argv)  # 拉回目标解释器重启自身


# 只在被当作入口执行时兜底；tests/ 会 import 本模块，import 时不该 exec 掉宿主进程。
if __name__ == "__main__":
    _ensure_runtime()  # 以下代码保证运行在目标 venv 里

import argparse  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import time  # noqa: E402
from typing import Any, Callable, Dict, List, Optional, Sequence  # noqa: E402


SCHEMA_VERSION = 1
READY_USER_STATUSES = {"ready", "needs_refresh"}
READY_BOT_STATUSES = {"ready"}
EXIT_CODES = {
    "ready": 0,
    "invalid_input": 19,
    "cli_missing": 20,
    "cli_broken": 21,
    "update_available": 22,
    "update_check_failed": 23,
    "config_required": 24,
    "login_required": 25,
    "account_mismatch": 26,
    "account_confirmation_required": 27,
    "scope_required": 28,
    "auth_check_failed": 29,
    "scope_check_failed": 30,
    "identity_unavailable": 31,
    "profile_required": 32,
}

# --- 网络抖动处理参数（ADR 0006 §3）---
# 总尝试 3 次（首次 + 2 次重试），退避 1s、2s。
NETWORK_MAX_ATTEMPTS = 3
TIMEOUT_RETURNCODE = 124        # subprocess.TimeoutExpired 的投影
EXEC_FAILURE_RETURNCODE = 127   # OSError：CLI 起不来（确定性失败）
# 无结构化错误信息时的兜底关键词（ADR 0006 §2：先结构化，退到关键词）。
TRANSIENT_HINTS = (
    "timed out",
    "timeout",
    "etimedout",
    "econnreset",
    "econnrefused",
    "econnaborted",
    "enotfound",
    "eai_again",
    "epipe",
    "connection reset",
    "connection refused",
    "connection closed",
    "socket hang up",
    "network error",
    "network is unreachable",
    "getaddrinfo",
    "dns",
    "tls handshake",
    "handshake failed",
    "fetch failed",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "too many requests",
)

CommandResult = Dict[str, Any]
Executor = Callable[[Sequence[str], float], CommandResult]
CliLocator = Callable[[str], Optional[str]]


def run_command_once(argv: Sequence[str], timeout: float) -> CommandResult:
    """跑一次 lark-cli，把三种结局统一成 CommandResult（不做重试）。"""
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or "command timed out"
        return {
            "returncode": TIMEOUT_RETURNCODE,
            "stdout": stdout.decode("utf-8", "replace") if isinstance(stdout, bytes) else stdout,
            "stderr": stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else stderr,
        }
    except OSError as exc:
        return {"returncode": EXEC_FAILURE_RETURNCODE, "stdout": "", "stderr": str(exc)}

    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def is_transient_failure(result: CommandResult) -> bool:
    """判定一次 lark-cli 调用是否属瞬时网络故障（ADR 0006 §2）。

    判定优先用结构化信息：subprocess 超时、CLI JSON 信封里的 ``error.type``
    与 HTTP 状态码；没有结构化信息才退到错误消息关键词匹配。鉴权失败、参数
    非法、CLI 起不来这类确定性失败一律判 False，重试它们必然同样失败。
    """
    returncode = result.get("returncode")
    if returncode == 0:
        return False
    if returncode == EXEC_FAILURE_RETURNCODE:
        # OSError：可执行文件不存在 / 没有执行权限，属确定性失败。
        return False
    if returncode == TIMEOUT_RETURNCODE:
        return True

    payload = parse_json_result(result)
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            error_type = str(error.get("type") or "").lower()
            if error_type in {"network", "timeout"}:
                return True
            try:
                status = int(error.get("code") or error.get("status") or 0)
            except (TypeError, ValueError):
                status = 0
            if status == 429 or 500 <= status <= 599:
                return True
            # 结构化信息明确说了不是网络类 → 确定性失败，不再看关键词。
            return False

    text = "%s\n%s" % (result.get("stdout", ""), result.get("stderr", ""))
    lowered = text.lower()
    return any(hint in lowered for hint in TRANSIENT_HINTS)


def execute_command(argv: Sequence[str], timeout: float) -> CommandResult:
    """所有 lark-cli 调用的唯一出口：单次超时 + 瞬时失败重试（ADR 0006）。

    预检的五个调用点（``--version``、``update --check``、``auth status``、
    ``auth list``、``auth check``）全是只读查询，天然幂等，重试安全
    （ADR 0006 §4）；确定性失败保持原有的终态 stage，不重试。
    """
    attempt = 1
    while True:
        result = run_command_once(argv, timeout)
        if result.get("returncode") == 0:
            return result
        if attempt >= NETWORK_MAX_ATTEMPTS or not is_transient_failure(result):
            return result
        wait_seconds = 2 ** (attempt - 1)  # 1s、2s（ADR 0006 §3）
        sys.stderr.write(
            "[retry] lark-cli %s 第 %d/%d 次尝试遇到瞬时网络故障"
            "（returncode=%s），%d 秒后重试\n"
            % (
                " ".join(str(item) for item in list(argv)[1:]),
                attempt,
                NETWORK_MAX_ATTEMPTS,
                result.get("returncode"),
                wait_seconds,
            )
        )
        time.sleep(wait_seconds)
        attempt += 1


def parse_json_result(result: CommandResult) -> Optional[Any]:
    for stream in (result.get("stdout", ""), result.get("stderr", "")):
        text = str(stream).strip()
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            continue
    return None


def parse_version(text: str) -> Optional[str]:
    match = re.search(
        r"(?:\bversion\s+|\bv)(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b",
        text,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def mask_open_id(open_id: str) -> str:
    if len(open_id) <= 10:
        return open_id
    return f"{open_id[:7]}...{open_id[-4:]}"


def finish(
    report: Dict[str, Any], stage: str, next_action: str, *, ok: bool = False
) -> Dict[str, Any]:
    report["ok"] = ok
    report["stage"] = stage
    report["next_action"] = next_action
    return report


def profile_command(cli_path: str, profile: Optional[str], *args: str) -> List[str]:
    command = [cli_path]
    if profile:
        command.extend(["--profile", profile])
    command.extend(args)
    return command


def is_not_configured(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("reason") == "not_configured":
        return True
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return error.get("type") == "configuration" or error.get("subtype") in {
        "not_configured",
        "config_missing",
    }


def is_missing_profile(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    message = str(error.get("message") or "").lower()
    return error.get("subtype") == "not_configured" and (
        "profile" in message and "not found" in message
    )


def extract_users(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("users"), list):
        return [item for item in payload["users"] if isinstance(item, dict)]
    return []


def dedupe_scopes(scopes: Sequence[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for group in scopes:
        for scope in group.replace(",", " ").split():
            if scope and scope not in seen:
                seen.add(scope)
                result.append(scope)
    return result


def is_externally_managed_identity(identity_payload: Dict[str, Any]) -> bool:
    message = str(identity_payload.get("message") or "").lower()
    hint = str(identity_payload.get("hint") or "").lower()
    return (
        "provided by " in message
        or "credential source" in message
        or "credential provider" in hint
    )


def run_preflight(
    *,
    identity: str,
    expected_open_id: Optional[str] = None,
    expected_name: Optional[str] = None,
    profile: Optional[str] = None,
    scopes: Optional[Sequence[str]] = None,
    allow_outdated: bool = False,
    allow_unknown_version: bool = False,
    accept_name_match: bool = False,
    cli_name: str = "lark-cli",
    timeout: float = 20.0,
    executor: Executor = execute_command,
    cli_locator: CliLocator = shutil.which,
) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "stage": "starting",
        "next_action": "inspect",
        "cli": {"installed": False},
        "auth": {"identity": identity},
    }

    if identity not in {"user", "bot"}:
        return finish(report, "invalid_input", "choose_identity")
    if identity == "bot" and (expected_open_id or expected_name or scopes):
        return finish(report, "invalid_input", "remove_user_only_options")

    cli_path = cli_locator(cli_name)
    if not cli_path:
        return finish(report, "cli_missing", "ask_install")

    report["cli"] = {"installed": True, "path": cli_path}
    version_result = executor([cli_path, "--version"], timeout)
    version_output = "\n".join(
        str(version_result.get(stream, "")) for stream in ("stdout", "stderr")
    )
    current_version = parse_version(version_output)
    if version_result.get("returncode") != 0 or not current_version:
        report["cli"]["version_check_exit_code"] = version_result.get("returncode")
        return finish(report, "cli_broken", "ask_reinstall")
    report["cli"]["current_version"] = current_version

    update_result = executor([cli_path, "update", "--check", "--json"], timeout)
    update_payload = parse_json_result(update_result)
    valid_update_payload = (
        isinstance(update_payload, dict)
        and update_payload.get("ok") is not False
        and (
            bool(update_payload.get("action"))
            or bool(update_payload.get("current_version"))
            or bool(update_payload.get("latest_version"))
            or bool(update_payload.get("current"))
            or bool(update_payload.get("latest"))
        )
    )
    if update_result.get("returncode") != 0 or not valid_update_payload:
        report["cli"]["update"] = {
            "status": "unknown",
            "check_exit_code": update_result.get("returncode"),
        }
        if not allow_unknown_version:
            return finish(
                report, "update_check_failed", "ask_continue_without_version_check"
            )
    else:
        update_current = str(
            update_payload.get("current_version")
            or update_payload.get("current")
            or current_version
        )
        latest_version = update_payload.get("latest_version") or update_payload.get(
            "latest"
        )
        action = str(update_payload.get("action", ""))
        update_available = action == "update_available" or (
            latest_version is not None and update_current != str(latest_version)
        )
        report["cli"]["update"] = {
            "status": "update_available" if update_available else "up_to_date",
            "current_version": update_current,
            "latest_version": str(latest_version or update_current),
            "update_command": "lark-cli update --json",
        }
        if update_available and not allow_outdated:
            return finish(report, "update_available", "ask_update_choice")

    if profile:
        report["auth"]["profile"] = profile
    auth_command = profile_command(cli_path, profile, "auth", "status", "--json")
    auth_result = executor(auth_command, timeout)
    auth_payload = parse_json_result(auth_result)
    if is_missing_profile(auth_payload):
        return finish(report, "profile_required", "choose_existing_profile")
    if is_not_configured(auth_payload):
        return finish(report, "config_required", "ask_configure")
    if auth_result.get("returncode") != 0 or not isinstance(auth_payload, dict):
        report["auth"]["check_exit_code"] = auth_result.get("returncode")
        return finish(report, "auth_check_failed", "inspect_auth_error")

    report["auth"]["app_id"] = auth_payload.get("appId")
    report["auth"]["brand"] = auth_payload.get("brand")
    identities = auth_payload.get("identities")
    selected = identities.get(identity) if isinstance(identities, dict) else None
    if not isinstance(selected, dict):
        return finish(report, "auth_check_failed", "inspect_auth_error")

    status = str(selected.get("status", "unknown"))
    available = bool(selected.get("available"))
    externally_managed = is_externally_managed_identity(selected)
    report["auth"]["status"] = status
    report["auth"]["available"] = available
    report["auth"]["managed_externally"] = externally_managed

    ready_statuses = READY_USER_STATUSES if identity == "user" else READY_BOT_STATUSES
    if not available or status not in ready_statuses:
        if externally_managed:
            return finish(
                report,
                "identity_unavailable",
                "repair_external_credential_provider",
            )
        if status == "not_configured":
            return finish(
                report,
                "identity_unavailable",
                "inspect_identity_policy_or_credentials",
            )
        if identity == "user" and status == "missing":
            return finish(report, "login_required", "start_device_login")
        return finish(report, "auth_check_failed", "inspect_auth_error")

    if identity == "bot":
        return finish(report, "ready", "run_lark_cli", ok=True)

    actual_open_id = str(selected.get("openId") or "")
    actual_name = str(selected.get("userName") or "")
    if not actual_open_id or not actual_name:
        list_command = profile_command(cli_path, profile, "auth", "list", "--json")
        list_result = executor(list_command, timeout)
        users = extract_users(parse_json_result(list_result))
        usable_users = [
            user
            for user in users
            if user.get("tokenStatus") in READY_USER_STATUSES
            or user.get("tokenStatus") == "valid"
        ]
        if len(usable_users) == 1:
            actual_open_id = str(usable_users[0].get("userOpenId") or "")
            actual_name = str(usable_users[0].get("userName") or "")

    if not actual_open_id:
        return finish(report, "login_required", "start_device_login")

    report["auth"]["user_name"] = actual_name or None
    report["auth"]["user_open_id_masked"] = mask_open_id(actual_open_id)

    if expected_open_id:
        if actual_open_id != expected_open_id:
            report["auth"]["account_match"] = "mismatch"
            return finish(report, "account_mismatch", "ask_replace_account")
        report["auth"]["account_match"] = "strong"
        if expected_name and actual_name and expected_name != actual_name:
            report["auth"]["account_name_warning"] = "open_id_matched_name_changed"
    elif expected_name:
        if actual_name != expected_name:
            report["auth"]["account_match"] = "mismatch"
            return finish(report, "account_mismatch", "ask_replace_account")
        report["auth"]["account_match"] = "weak"
        if not accept_name_match:
            return finish(
                report,
                "account_confirmation_required",
                "ask_confirm_name_match",
            )
    else:
        report["auth"]["account_match"] = "not_requested"

    required_scopes = dedupe_scopes(scopes or [])
    if required_scopes:
        report["auth"]["required_scopes"] = required_scopes
        if externally_managed:
            report["auth"]["scope_check"] = "delegated_to_external_provider"
            return finish(report, "ready", "run_lark_cli", ok=True)
        scope_value = " ".join(required_scopes)
        scope_command = profile_command(
            cli_path, profile, "auth", "check", "--scope", scope_value, "--json"
        )
        scope_result = executor(scope_command, timeout)
        scope_payload = parse_json_result(scope_result)
        if not isinstance(scope_payload, dict):
            report["auth"]["scope_check_exit_code"] = scope_result.get("returncode")
            return finish(report, "scope_check_failed", "inspect_scope_error")
        scope_error = scope_payload.get("error")
        if isinstance(scope_error, str) and scope_error in {
            "not_logged_in",
            "no_token",
        }:
            return finish(report, "login_required", "start_device_login")
        if "missing" not in scope_payload:
            report["auth"]["scope_check_exit_code"] = scope_result.get("returncode")
            return finish(report, "scope_check_failed", "inspect_scope_error")
        missing = scope_payload.get("missing") or []
        if not isinstance(missing, list):
            return finish(report, "scope_check_failed", "inspect_scope_error")
        report["auth"]["missing_scopes"] = missing
        if missing:
            return finish(report, "scope_required", "authorize_missing_scopes")

    return finish(report, "ready", "run_lark_cli", ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only preflight checks before using lark-cli"
    )
    parser.add_argument("--identity", choices=("user", "bot"), required=True)
    parser.add_argument("--expected-open-id")
    parser.add_argument("--expected-name")
    parser.add_argument("--profile")
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="required user scope; repeat or pass a space/comma-separated group",
    )
    parser.add_argument(
        "--allow-outdated",
        action="store_true",
        help="continue only after the user explicitly chose to skip an available update",
    )
    parser.add_argument(
        "--allow-unknown-version",
        action="store_true",
        help="continue only after the user accepted an unavailable update check",
    )
    parser.add_argument(
        "--accept-name-match",
        action="store_true",
        help="continue only after the user confirmed a display-name-only match",
    )
    parser.add_argument("--cli", default="lark-cli", help="lark-cli executable name")
    parser.add_argument("--timeout", type=float, default=20.0)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_preflight(
        identity=args.identity,
        expected_open_id=args.expected_open_id,
        expected_name=args.expected_name,
        profile=args.profile,
        scopes=args.scope,
        allow_outdated=args.allow_outdated,
        allow_unknown_version=args.allow_unknown_version,
        accept_name_match=args.accept_name_match,
        cli_name=args.cli,
        timeout=args.timeout,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_CODES.get(str(report.get("stage")), 1)


if __name__ == "__main__":
    sys.exit(main())
