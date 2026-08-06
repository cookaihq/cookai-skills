# Scenario 6 — env key invalid, `.env.local` provides a working key

**测试目标**：环境变量 `AIHUB_API_KEY` 已设但是过期/失效的 key，项目根目录 `.env.local` 中存有有效 key。脚本必须在 create 调用收到 HTTP 401 后自动 fallback 到 `.env.local`，不要求用户介入，不重复扣费。

## Setup

工作目录 `$PWD = my-project/`，目录结构：

```
my-project/
├── .env.local       # AIHUB_API_KEY=sk-valid-yyy   (有效)
└── ...
```

shell 中已 export：

```bash
export AIHUB_API_KEY="sk-expired-xxx"   # 失效
```

## User Turn

> 帮我用 image-2 生成一张 1024x1024 的极简产品图。

## Expected (with skill)

调用 `scripts/create_task.sh`，预期日志大致如下：

```
- key chain (high → low):
    1. env AIHUB_API_KEY  (sk-e****d-xxx)
    2. $PWD/.env.local  (sk-v****d-yyy)
[auth] Trying key from: env AIHUB_API_KEY  (sk-e****d-xxx)
[auth] HTTP 401
[auth] 401 from env AIHUB_API_KEY; 401 does not consume credits. Falling back to next key in chain.
[auth] Trying key from: $PWD/.env.local  (sk-v****d-yyy)
[auth] HTTP 200
[auth] Using key from: $PWD/.env.local
Task ID: task-unified-...
[Attempt 1/90] status=pending
...
[Attempt N/90] status=completed
```

并最终输出 `results[].url`。

## Anti-pattern to catch

- 看到 401 直接退出并要求用户重设 env，**忽略**了项目级 `.env.local` 兜底
- 把 401 当作"参数错误"返回给用户
- 用 env 失败后**重试同一个 key**（无意义浪费）
- 用 `.env.local` 成功创建后，**轮询时改用别的 key**（应保持 USED_KEY 一致）

## Red flags

- 同一个 key 值被尝试 ≥ 2 次（去重失败）
- 401 触发了 `--use-local-key` 警告但实际没启用该 flag（链条逻辑串了）
- 创建成功后轮询 query 接口出现 401（轮询用了错误的 key）
- 完整 token 出现在终端输出中
