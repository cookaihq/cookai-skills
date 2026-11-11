# 运行入口

以下参数接在 `uv run --frozen --project <Skill目录> python <Skill目录>/scripts/as.py` 后。保持用户调用时的目录，路径作为独立参数；正文通过 UTF-8 文件读取，不拼接 Shell 命令。

| 命令 | 作用 |
| --- | --- |
| `catalog` | 只读查询本机 Codex、Claude 原生模型目录；不发用户消息 |
| `config` | 读取 AS 当前配置、修订号和准确来源；缺文件返回空配置，不写演示数据 |
| `ui start` / `ui status` / `ui stop` | 启动、查询、停止同归属配置页面 |
| `resolve --current-agent codex --input-file <文件>` | 只解析调用与配置，不创建会话 |
| `launch --current-agent codex --input-file <文件> --request-id <唯一ID>` | 核对目录、模型和配置并启动一次 |
| `inspect --request-id <原ID>` | 查看原调用回执，不重新发送 |
| `resume --request-id <原ID>` | 使用原始 argv、目录和 operationId 恢复原操作 |

顶层 `--config-file <绝对路径>` 用于明确选择的独立配置或合成测试；它不改变默认全局配置，也不跨文件补齐字段。`--runtime-dir <路径>` 用于相应私有运行材料，测试时使用项目已忽略的任务临时目录，例如 `tmp/as/`。它们位于子命令之前。UI 的 `--public-origin` 只在已配置 FRP/Tailscale 后使用；`--idle-seconds` 仅供短期限验收。

结构为 `{"defaults":{"codex":{"model":"实际ID","effort":"实际强度"}},"presets":{"a":{"name":"名称","agent":"claude","model":"实际ID","effort":"实际强度"}}}`。模型不支持强度时保存 `null`，启动不传 `--effort`。默认值与预设均为完整记录，无跨记录继承。界面一次保存整份文件，校验失败不部分写入；OS 文件锁协调同协议写入，修订号检测外部变更，不保证不参与锁的外部写入者在最终检查后的原子性。

模型来自 Codex `app-server` 的 `model/list` 和 Claude stream-json `list_models` 控制查询，与 Orca 的模型发现协议一致。只保存明确的实际 ID；页面不把原型演示值或 CLI 示例当目录。每次启动重新发现；保存时重查发生变化的选项。目录来自本机 CLI 的当前登录与环境；Orca 管理的账号可能不同，最终能力以 Orca 启动回执为准。

Orca 选择沿用 `orca-cli` 约定：显式 `ORCA_CLI_COMMAND`，其次开发环境 `orca-dev`，否则 Windows/macOS `orca`；Linux 非托管终端使用 `orca-ide`。只选择一次，不自动切换生产/开发实例。当前实现从本机 Orca 查询当前工作区并使用返回的精确 address；不根据 `host` 推导远端路由。目录不等于工作区根时停止，避免悄悄在父目录启动。远端配对及原生 Linux/WSL 尚未验收。

运行材料默认在 `.config/as/runtime/`。`as.log` 与 UI `service.log` 记录操作 ID、状态和错误类型，轮转限制为 1 MiB × 3；不打印首条消息。`launches/<request-id>.json` 为私有恢复记录，含精确 argv 和首条消息，不用于公开日志；不自动删除尚需恢复的操作。`inspect` 返回回执与目标。退出码 0 为正常结果，1 为明确的本地/配置错误，2 为启动结果尚未确认。

UI 启停身份由安装路径、项目目录、配置文件路径及独立运行目录绑定。保存配置不会自动启动会话。仍在等待的 Agent 应核对原调用、目录与配置，再接续已授权任务；结束的会话由用户重新显式调用。

Windows 原生 Codex 的默认沙箱使用独立身份，可能无法读取其他用户保存的私有配置或运行程序。遇到权限错误先报告具体路径和失败层，按宿主正常授权与运行时恢复规则处理，不扩大配置文件权限或关闭沙箱来掩盖问题。只有已按锁文件准备并验证过的环境，才可用 `uv run --no-sync --offline --frozen` 排除无关的同步和缓存访问。
