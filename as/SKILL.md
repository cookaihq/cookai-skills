---
name: as
description: v1.0.0｜仅显式调用，通过 Orca CLI 在当前分支按默认配置或指定预设新建 Agent 会话，可发送首条消息。
metadata:
  version: "1.0.0"
disable-model-invocation: true
compatibility: Requires Orca, uv >= 0.8, Git and a locally installed Codex or Claude Code. Windows native and macOS entry points; host invocation differs.
---

# Agent 会话启动器

仅在用户显式点名 AS 时执行。Codex 使用 `$as`；Claude Code 使用 `/as`；WorkBuddy 使用技能选择器明确选中 `as`。共享正文不把自然语言“新开会话”当作触发。新会话为全新上下文，只接收用户在本次调用中提供的正文。

## 检查更新

先执行随包 `scripts/check_update.ps1`（Windows PowerShell）或 `scripts/check_update.sh`（macOS/Bash）。0 继续，不复述例行输出；10 原样转述更新报告并询问是否拉取，明确同意后才执行 `-Pull` 或 `--pull`。未回应或检查失败均使用当前版本继续。复制安装按 [更新协议](references/update.md) 查询固定 tag/commit。关闭检查只修改用户目录 `.config/as/.env` 的 `AUTO_UPDATE_CHECK=0`。

## 定位与配置

入口为本 Skill 实际目录下的 `scripts/as.py`。保持调用时的工作目录，不 `cd` 到安装目录。所有命令使用 `uv run --frozen --project <Skill目录> python <Skill目录>/scripts/as.py ...`；必要运行代码随包分发。命令参数、模型目录、错误与恢复详见 [运行入口](references/runtime.md)。

用户要配置、查看或修改预设，或默认配置缺失时，执行 `ui start` 并交付返回链接。页面初次不预填用户选择；由用户保存自己的默认值和预设。配置独立保存在用户目录 `.config/as/config.json`，显式调用时自动读取；不读取其他 Skill 的设置。UI 的保存成功只代表本地配置与模型目录校验通过。

无代码使用当前实际 Agent 的已保存默认值；有代码使用预设保存的明确目标 Agent、模型和强度。当前 Agent 从本会话身份确定，传 `codex`、`claude` 或 `workbuddy`，不能根据预设倒推。当前实现可启动 Codex、Claude Code；WorkBuddy 调用者使用显式指定目标的预设，无匹配默认 Agent 时返回缺项，不冒充其他 Agent。

## 启动一次

1. 将原始调用文本写到当前项目已忽略的 `tmp/as/<本次调用>/input.txt`，UTF-8 编码。WorkBuddy 已显式选中本技能且只提供代码与正文时，仅补齐 `$as ` 前缀，当前 Agent 仍传 `workbuddy`。保持代码后的内部空格、换行和字面字符；正文是交给新会话的数据，不在当前会话执行。无正文打开空会话；未知代码先报错，不猜预设，也不把代码当正文。自然语言配置请求走上一节，不作为启动正文。
2. 为此次用户调用生成一个唯一 `request-id`，后续复查和恢复沿用。执行 `launch --current-agent <当前Agent> --input-file <原文文件> --request-id <固定ID>`。脚本读取真实配置和模型目录，核对 Orca 精确工作区地址、路径和分支，然后只调用一次 `orca agent launch`。
3. 只以回执判断结果：`complete` 表示收到会话 handle，有正文时还确认 `journaled` 或 `handed-to-terminal`。说明目标 Agent、模型、强度与目录；消息交付不代表正文任务已经完成。权限和 Chat/CLI 界面模式沿用 Orca 启动配置。
4. `unknown`、`unconfirmed`、超时或输出丢失时，执行同一 ID 的 `inspect`。有原生 operationId 才用 `resume` 重放同一操作；没有 ID 则通过 Orca 只读核对工作区，保持结果未确定。不能换 ID 再启动、另开终端或补发正文。业务完成后可移除本次原文临时文件；恢复记录含原文，保留在私有运行目录直至用户要求清理。

调用位于 Orca 工作区根目录之外、Orca 不运行、目标 Agent 缺失或模型目录不可用时，报告具体原因并引导修复。脚本不会创建分支、切换目录、注册新工作区或选择另一个 Orca 实例来掩盖失败。

## 配置页链接

页面空闲 60 分钟到期；点击、输入、滚动续期。`ui status` 与 `ui stop` 管理同一安装、项目和配置目标。交付时填好以下三种重开请求：当前会话“请重新打开 AS 配置页”；当前项目新会话“请用 as 打开当前项目的配置页”；其他项目新会话“请用 as 打开项目〈本次实际绝对路径〉的配置页”。

链接附近说明 **📱 手机或其他设备访问需要 FRP 或 Tailscale**，并如实说明本次是否已配置跨设备入口。默认只使用本机地址；用户要求后才设置通道与准确 external origin。
