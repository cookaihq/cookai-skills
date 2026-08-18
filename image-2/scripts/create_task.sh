#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# 解释器由 uv 按 <skill>/pyproject.toml + uv.lock 钉死，venv 落 <skill>/.venv
# （ADR 0007 §1.4 示例侧）。禁止改回裸 python3：那会按 PATH 解析到系统解释器。
# image_task.py 顶部还有 bootstrap 兜底，绕过本入口直接跑也会被拉回同一个 venv。
# --no-dev：uv run 会先把项目环境同步到位，默认连 dev 依赖组（pytest 等）一起装。
# 那些只服务本仓测试，不该进终端用户的 <skill>/.venv。
exec uv run --project "${SKILL_DIR}" --no-dev python "${SCRIPT_DIR}/image_task.py" "$@"
