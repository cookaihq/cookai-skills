"""create_task.sh 的离线 CLI 测试。

不发任何真实请求：把一个假 `curl` 和一个假 `sleep` 放在 PATH 最前面，用脚本文件
驱动每次调用返回什么。三条被测分支：

  1. H2 —— 轮询响应体为空时不再崩溃（命令替换剥尾换行 → 状态码被当 JSON 解析）
  2. M1 —— Retry-After 超上限时钳到 60s，而不是丢弃回落成 1s
  3. D2 —— 轮询墙钟预算耗尽时按终态收场，而不是把 90 轮全跑完

假 sleep 只记录被要求的秒数、立即返回，所以 1/2 秒级即可跑完；只有墙钟那条用
假 curl 里的真实 `/bin/sleep` 制造耗时。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "create_task.sh"

# 假 curl：按调用序号从 responses 目录取一份「状态码 / 响应头 / 响应体」。
# 支持 --dump-header（写响应头文件）与 --write-out '\n%{http_code}'。
FAKE_CURL = r"""#!/bin/bash
# 假 curl：不联网，按调用序号回放预置响应。
set -u
STATE_DIR="${FAKE_HTTP_DIR}"
COUNTER="${STATE_DIR}/counter"
n=$(cat "${COUNTER}" 2>/dev/null || echo 0)
n=$((n + 1))
printf '%s' "${n}" > "${COUNTER}"

header_file=""
url=""
prev=""
for arg in "$@"; do
  case "${prev}" in
    --dump-header) header_file="${arg}" ;;
    --output) out_file="${arg}" ;;
  esac
  case "${arg}" in
    http*) url="${arg}" ;;
  esac
  prev="${arg}"
done
printf '%s\n' "${url}" >> "${STATE_DIR}/urls"

resp_dir="${STATE_DIR}/${n}"
if [[ ! -d "${resp_dir}" ]]; then
  resp_dir="${STATE_DIR}/default"
fi
if [[ ! -d "${resp_dir}" ]]; then
  echo "fake curl: no scripted response for call ${n} (${url})" >&2
  exit 7
fi

if [[ -f "${resp_dir}/delay" ]]; then
  /bin/sleep "$(cat "${resp_dir}/delay")"
fi
if [[ -n "${header_file}" ]] && [[ -f "${resp_dir}/headers" ]]; then
  cat "${resp_dir}/headers" > "${header_file}"
elif [[ -n "${header_file}" ]]; then
  printf 'HTTP/1.1 200 OK\r\n\r\n' > "${header_file}"
fi
if [[ -f "${resp_dir}/exit" ]]; then
  exit "$(cat "${resp_dir}/exit")"
fi

code="$(cat "${resp_dir}/code")"
body=""
[[ -f "${resp_dir}/body" ]] && body="$(cat "${resp_dir}/body")"
# create 走 $'\n%{http_code}'（码在末尾）；poll 也一样。照 curl 的行为：先写 body
# 再写 --write-out 模板。
printf '%s' "${body}"
printf '\n%s' "${code}"
"""

FAKE_SLEEP = r"""#!/bin/bash
# 假 sleep：只记录被要求的秒数，立即返回。
printf '%s\n' "$1" >> "${FAKE_HTTP_DIR}/sleeps"
exit 0
"""


def _write_exec(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


class Harness:
    """假 curl / 假 sleep 的驱动器：预置每次调用的响应，再跑脚本、读回观测量。"""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.bin_dir = tmp_path / "bin"
        self.bin_dir.mkdir()
        self.state_dir = tmp_path / "http"
        self.state_dir.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        _write_exec(self.bin_dir / "curl", FAKE_CURL)
        _write_exec(self.bin_dir / "sleep", FAKE_SLEEP)

    def scripted(self, call, *, code="200", body="", headers=None,
                 delay=None, exit_code=None) -> None:
        """给第 `call` 次 curl 调用（1 起）预置一份响应；call="default" 是兜底。"""
        directory = self.state_dir / str(call)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "code").write_text(str(code))
        (directory / "body").write_text(body)
        if headers is not None:
            (directory / "headers").write_text(headers)
        if delay is not None:
            (directory / "delay").write_text(str(delay))
        if exit_code is not None:
            (directory / "exit").write_text(str(exit_code))

    def run(self, *args, timeout=120) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env['PATH']}"
        env["FAKE_HTTP_DIR"] = str(self.state_dir)
        env["AIHUB_API_KEY"] = "sk-fake-test-key-0123456789"
        env["HOME"] = str(self.home)
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            cwd=self.tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )

    def sleeps(self) -> list:
        path = self.state_dir / "sleeps"
        if not path.exists():
            return []
        return [line for line in path.read_text().splitlines() if line]

    def call_count(self) -> int:
        path = self.state_dir / "counter"
        return int(path.read_text()) if path.exists() else 0


@pytest.fixture
def harness(tmp_path):
    return Harness(tmp_path)


# --------------------------------------------------------------------------- #
# H2：轮询响应体为空
# --------------------------------------------------------------------------- #

def test_empty_poll_body_does_not_crash_and_keeps_the_task_id(harness):
    """回归 H2：`$( )` 剥掉尾换行后，"200\\n" 会塌成 "200"。

    旧实现会把状态码当响应体喂给 json.loads，得到 int 200，紧接着 data.get(...)
    抛 AttributeError —— 脚本连同已创建的 task_id 一起崩掉。
    """
    harness.scripted(1, code="200", body='{"id":"task-empty-body"}')
    harness.scripted(2, code="200", body="")           # 空响应体
    harness.scripted(3, code="200", body='{"status":"failed","error":{"message":"upstream"}}')

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert "AttributeError" not in result.stderr, result.stderr
    assert "Traceback" not in result.stderr, result.stderr
    # 空响应体那轮按「拿不到状态」处理，继续轮询，最终读到 failed 终态。
    assert result.returncode == 2, result.stdout + result.stderr
    assert "Task ID: task-empty-body" in result.stdout
    assert "status=unknown" in result.stdout, "空响应体应记为 unknown 而不是崩掉"
    assert "Task failed." in result.stdout


def test_empty_poll_body_then_completed_still_reaches_terminal(harness):
    harness.scripted(1, code="200", body='{"id":"task-empty-then-ok"}')
    harness.scripted(2, code="200", body="")
    harness.scripted(
        3, code="200",
        body='{"status":"completed","results":[{"url":"https://x.test/a.png","content_type":"image/png"}]}',
    )

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Task completed." in result.stdout
    assert "task-empty-then-ok" in result.stdout


# --------------------------------------------------------------------------- #
# M1：Retry-After 钳制
# --------------------------------------------------------------------------- #

def test_oversized_retry_after_is_clamped_not_discarded(harness):
    """回归 M1：超上限时钳到 60s，而不是丢弃后回落成 1s 的指数退避。"""
    harness.scripted(1, code="429", body='{"error":"rate limited"}',
                     headers="HTTP/1.1 429 Too Many Requests\r\nRetry-After: 99999\r\n\r\n")
    harness.scripted(2, code="200", body='{"id":"task-clamped"}')
    harness.scripted(3, code="200", body='{"status":"failed","error":{"message":"stop"}}')

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert "60s 后重试" in result.stderr, result.stderr
    assert harness.sleeps()[0] == "60", harness.sleeps()
    assert result.returncode == 2
    assert "Task ID: task-clamped" in result.stdout


def test_reasonable_retry_after_is_honoured_verbatim(harness):
    harness.scripted(1, code="429", body='{"error":"rate limited"}',
                     headers="HTTP/1.1 429 Too Many Requests\r\nRetry-After: 7\r\n\r\n")
    harness.scripted(2, code="200", body='{"id":"task-ra-7"}')
    harness.scripted(3, code="200", body='{"status":"failed","error":{"message":"stop"}}')

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert harness.sleeps()[0] == "7", harness.sleeps()
    assert result.returncode == 2


def test_http_date_retry_after_falls_back_to_backoff(harness):
    harness.scripted(1, code="429", body='{"error":"rate limited"}',
                     headers=("HTTP/1.1 429 Too Many Requests\r\n"
                              "Retry-After: Wed, 21 Oct 2026 07:28:00 GMT\r\n\r\n"))
    harness.scripted(2, code="200", body='{"id":"task-ra-date"}')
    harness.scripted(3, code="200", body='{"status":"failed","error":{"message":"stop"}}')

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert harness.sleeps()[0] == "1", "HTTP-date 形式回落到指数退避起点 1s"
    assert result.returncode == 2


# --------------------------------------------------------------------------- #
# D2：轮询墙钟预算
# --------------------------------------------------------------------------- #

def test_polling_stops_when_the_wall_clock_budget_is_spent(harness):
    """回归 D2：次数没跑完，但 max-attempts × poll-interval 的墙钟预算已耗尽。

    预算 = 5 × 1s = 5s；每次查询由假 curl 真实耗时 2s，所以第 3 次查询后就超预算，
    剩下的轮次不再发出。
    """
    harness.scripted(1, code="200", body='{"id":"task-wallclock"}')
    harness.scripted("default", code="200", body='{"status":"processing"}', delay=2)

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "wall-clock budget was spent" in result.stdout, result.stdout
    # 1 次 create + 少于 5 次轮询（预算在第 5 轮之前就到期）。
    assert harness.call_count() < 6, harness.call_count()
    assert "task-wallclock" in result.stdout


def test_count_budget_still_ends_polling_when_wall_clock_is_untouched(harness):
    """快速响应下墙钟用不完，仍按次数预算收场（原有行为不能被改坏）。"""
    harness.scripted(1, code="200", body='{"id":"task-count-budget"}')
    harness.scripted("default", code="200", body='{"status":"processing"}')

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "3")

    assert result.returncode == 3, result.stdout + result.stderr
    assert "Polling timed out after 3 attempts." in result.stdout
    assert harness.call_count() == 4  # 1 次 create + 3 次轮询


# --------------------------------------------------------------------------- #
# 附带：确定性 4xx / 空体的确定性错误不应把状态码当 JSON
# --------------------------------------------------------------------------- #

def test_deterministic_poll_error_with_empty_body_reports_the_status_code(harness):
    harness.scripted(1, code="200", body='{"id":"task-403"}')
    harness.scripted(2, code="403", body="")

    result = harness.run("--prompt", "a cat", "--no-save",
                         "--poll-interval", "1", "--max-attempts", "5")

    assert result.returncode == 1
    assert "HTTP 403" in result.stderr, result.stderr
    assert "task-403" in result.stderr, "确定性失败也要把 task_id 交回用户"
    assert "Traceback" not in result.stderr
