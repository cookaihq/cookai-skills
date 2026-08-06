# Scenario 3 — env key 失效 fallback 到 .env.local

**目标**：进程 env 里的 key 返回 401 时，自动尝试 `$PWD/.env.local` 里的下一把 key。

## Setup
- `export AIHUB_API_KEY=sk-invalid`（会 401）
- `$PWD/.env.local` 内有一把有效 `AIHUB_API_KEY=sk-valid`

## Expected (with skill)
- 先用 sk-invalid → 401 → 自动换 sk-valid → 成功
- 全程 key 掩码；401 不消耗积分故 fallback 安全

## Red flags
- 401 后直接放弃、不尝试 .env.local
- 把完整 key 打印出来
