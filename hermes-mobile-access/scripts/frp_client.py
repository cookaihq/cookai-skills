"""Call the versioned FRP CLI, without importing its credential/process internals."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
import subprocess

from configuration import ConfigError


class FrpClient:
    def __init__(self, skill, project, home, connection_id=None):
        self.skill, self.project, self.home = map(lambda p: Path(p).expanduser().resolve(), (skill, project, home))
        self.connection_id = connection_id

    def call(self, command, *arguments, timeout=25):
        entry = self.skill / "scripts/frpc_launch.py"
        if not entry.is_file():
            raise ConfigError("frp_unavailable", "未找到 FRP Launch 公开入口；请安装带消费者接口的版本。", self.skill)
        argv = ["uv", "run", "--frozen", "--project", str(self.skill), "python", "-B", str(entry),
                "--project", str(self.project), "--home", str(self.home), "--json"]
        if self.connection_id:
            argv += ["--consumer", "hermes-mobile-access", "--instance", self.connection_id]
        try:
            process = subprocess.run(argv + [command, *map(str, arguments)], capture_output=True,
                env=dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1"), timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            raise ConfigError("frp_timeout", "FRP 操作超时；状态未知，须查询后再决定是否重试。") from None
        except OSError as exc:
            # Preserve the failing layer without forwarding stderr, command
            # contents or any provider credential values into the report.
            raise ConfigError("frp_process_start_failed", "FRP 调用工具无法启动：%s，errno=%s，winerror=%s。请核对 uv 路径与执行权限。" %
                (type(exc).__name__, exc.errno, getattr(exc, "winerror", None))) from None
        try:
            data = json.loads(process.stdout)
        except (ValueError, TypeError):
            raise ConfigError("frp_protocol", "FRP 入口未返回有效结构化结果（退出码 %s）；请检查版本和运行环境。" % process.returncode) from None
        if not isinstance(data, dict):
            raise ConfigError("frp_protocol", "FRP 接口响应类型无效。")
        if process.returncode:
            # Raw stderr and provider messages are not forwarded into UI/logs.
            reason = data.get('result', '')
            reason = reason if isinstance(reason, str) and re.fullmatch('[a-z_]{1,64}', reason) else 'rejected'
            raise ConfigError("frp_" + reason, "FRP %s 未通过（%s），请通过 FRP Launch 诊断对应来源。" % (command, reason))
        if command == "context-info" and data.get("interface_version") != 1:
            raise ConfigError("frp_protocol", "需要 FRP 消费者接口版本 1。")
        return data

    def context(self):
        return self.call("context-info")

    def configure(self, revision, prefix, port):
        return self.call("instance-config", "--context-revision", revision,
                         "--domain-prefix", prefix, "--local-port", port, "--require-tls")

    def start(self):
        result = self.call("start", "--mode", "official", "--wait", 15,
                           "--owner-pid", os.getpid(), timeout=55)
        if result.get("result") not in ("ok", "already_running"):
            raise ConfigError("frp_start_unverified", "FRP 启动尚未验证。")
        return result

    def status(self):
        if not self.connection_id:
            return {"modes": {"official": {"running": False}}, "public_endpoint": "not_checked"}
        return self.call("status")

    def stop(self):
        if not self.connection_id:
            raise ConfigError("selection_required", "停止连接需要稳定连接 ID。")
        stopped = self.call("stop", timeout=30)
        outcomes = stopped.get("modes", {})
        if not outcomes or any(item.get("result") not in ("stopped", "not_running")
                and not (item.get('result') == 'stale_pid_cleaned' and item.get('owned_process_exited') is True)
                for item in outcomes.values()):
            raise ConfigError("cleanup_unverified", "停止遇到过期或不匹配的归属记录；尚未证实本连接资源全部停止。")
        result = self.status()
        running = result.get("modes", {}).get("official", {}).get("running")
        if running is True:
            raise ConfigError("cleanup_failed", "停止后仍检测到本连接进程；入口关闭尚未证实。")
        if running is not False:
            raise ConfigError("cleanup_unverified", "停止后的进程状态无法核对；尚未证实本连接资源全部停止。")
        return result
