"""入口解释器 bootstrap（ADR 0007 §1.4）。

作用：进程启动时把「用哪个解释器跑」从调用方每轮的记忆变成结构性事实——
不在目标 venv 就 `os.execv` 拉回去，venv 缺失就按 `uv.lock` 自动重建。

**只用 Python 3.9 兼容语法、只用 stdlib**：本模块会被系统 python3（本机 3.9.6）
先执行，用了新语法会在 SyntaxError 阶段就死掉，兜底反而成了故障点。

入口调用方式（保持在 `__main__` 语义下，脚本被 import 时不触发 execv——
tests/runtime/*.py 会 exec_module 导入 image_task 做单测）：

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if __name__ == "__main__":
        import _runtime_bootstrap
        _runtime_bootstrap.ensure()
"""

import os
import shlex
import subprocess
import sys

# 一次性再入护栏：exec 之后仍不在目标 venv，说明 venv 目录本身坏了（例如
# bin/python 在但不是该 venv 的解释器）。没有这个标记会无限 execv 且零输出。
# 标记值存的是**本轮的目标 venv realpath**，不是布尔——变量被外部环境 export
# 时，值不匹配就不算本轮的再入，仍照常自动重建；嵌套调用不同目标的入口也因此
# 天然区分开。
REEXEC_ENV = "IMAGE_2_BOOTSTRAP_REEXEC"

UV_INSTALL_HINT = "curl -LsSf https://astral.sh/uv/install.sh | sh"

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 重定位只认 uv 原生的 UV_PROJECT_ENVIRONMENT，且**基准必须是项目根**——uv
# 0.8 把相对值按项目根解析，按 CWD 解析会在用户自己的项目目录里设了
# `UV_PROJECT_ENVIRONMENT=.venv` 时把进程 exec 进用户项目的 venv。
# 绝对值不受影响：os.path.join 遇到绝对路径直接返回它。
VENV_DIR = os.path.join(SKILL_DIR, os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")
VENV_PY = os.path.join(VENV_DIR, "bin", "python")


def _fail(msg):
    sys.stderr.write(msg + "\n")
    raise SystemExit(1)


def _manual_rebuild_hint():
    # 必须 shell 引用：路径含空格时，未引用的 `rm -rf /tmp/sp ace/.venv` 被照抄
    # 执行会删掉两个无关路径。
    # --no-dev：dev 依赖组（pytest 等）只服务本仓的测试，终端用户的运行环境里不该
    # 出现；不加这个开关 uv 会默认把 dev 组一起装进 <skill>/.venv。
    return "rm -rf %s && uv sync --project %s --no-dev" % (
        shlex.quote(VENV_DIR), shlex.quote(SKILL_DIR)
    )


def _venv_is_valid():
    """有效 venv = 解释器在 + pyvenv.cfg 在。

    只判 bin/python 存在是不够的：sync 中断、手工建的同名目录、残留软链都会
    让解释器存在而目录不是 venv，此时跳过修复直接 execv 会陷入无限重启。
    """
    return (os.path.exists(VENV_PY)
            and os.path.exists(os.path.join(VENV_DIR, "pyvenv.cfg")))


def _require_uv():
    """uv 是系统级程序，缺失/版本过低只报错给命令，不擅自安装（ADR 0007 §4.2）。"""
    try:
        probe = subprocess.run(["uv", "--version"], stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=30)
    except (OSError, subprocess.SubprocessError):
        _fail("uv 未安装。请执行：" + UV_INSTALL_HINT)
    parts = probe.stdout.decode("utf-8", "replace").split()  # "uv 0.8.11 (...)"
    found = parts[1] if len(parts) > 1 else "0"
    try:
        numeric = tuple(int(x) for x in (found.split(".") + ["0", "0"])[:2])
    except ValueError:
        # uv 出错时 stdout 是 "error: ..." 之类，硬 int() 会抛未捕获的
        # ValueError；按「版本不可用」处理，仍走可复制命令的报错。
        numeric = (0, 0)
    if numeric < (0, 8):
        _fail("uv 版本过低（需 >= 0.8，当前 %s）。请执行：uv self update" % found)


def _sync():
    """按 uv.lock 冻结重建 skill 自有环境（ADR 0007 §4.1：自动修复）。"""
    sys.stderr.write("[bootstrap] 运行环境缺失，正在按 uv.lock 重建 %s ...\n" % VENV_DIR)
    try:
        sync = subprocess.run(["uv", "sync", "--project", SKILL_DIR, "--no-dev"],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=600)  # 网络调用必设总预算（ADR 0006）
    except subprocess.TimeoutExpired:
        _fail("uv sync 超过 600 秒未完成，疑似网络异常。请手工执行："
              + _manual_rebuild_hint())
    if sync.returncode != 0 or not _venv_is_valid():
        _fail("uv sync 失败，无法重建运行环境（请手工执行：%s）：\n%s"
              % (_manual_rebuild_hint(),
                 sync.stdout.decode("utf-8", "replace")))


def ensure():
    """确保当前进程运行在目标 venv 里；不是则 exec 拉回去，缺失则先重建。"""
    target = os.path.realpath(VENV_DIR)
    if os.path.realpath(sys.prefix) == target:
        # 已在目标 venv：清掉本轮标记，避免派生的子进程继承后误判。
        if os.environ.get(REEXEC_ENV) == target:
            os.environ.pop(REEXEC_ENV, None)
        return
    if os.environ.get(REEXEC_ENV) == target:
        # 只认「值等于本轮目标」才算再入：外部 export 了同名变量、或嵌套调用的
        # 是另一个目标时，这里不成立，仍照常走下面的自动重建。
        _fail("运行环境异常：已重启到 %s 但解释器仍不在该 venv 内，目录疑似损坏。\n"
              "请手工重建：%s" % (VENV_DIR, _manual_rebuild_hint()))
    if not _venv_is_valid():
        _require_uv()
        _sync()
    os.environ[REEXEC_ENV] = target  # putenv，execv 后的进程能读到
    os.execv(VENV_PY, [VENV_PY] + sys.argv)  # 拉回目标解释器重启自身
