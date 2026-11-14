---
name: as
description: v1.2.0｜仅显式调用，通过 Orca CLI 在当前分支按默认配置或指定预设新建 Agent 会话，可发送首条消息。
metadata:
  version: "1.2.0"
disable-model-invocation: true
compatibility: Requires Orca, uv >= 0.8, Git and a locally installed Codex or Claude Code. Windows native and macOS entry points; host invocation differs.
---

# Agent 会话启动器

## 配置页面快捷入口

先分流：显式调用本 Skill 后，内容去掉首尾空白后恰为 `配置`、`配置页面`、`打开配置`、`打开配置页面`、`设置`、`设置页面`、`打开设置`、`打开设置页面` 中任一项时，在当前会话直接按[运行入口](references/runtime.md#配置页面快捷入口)执行 `ui start`，返回有效链接与实际期限后结束。此窄例外先于更新检查及“启动一次”，不创建会话、不准备启动正文，也不先查询 Orca 或模型目录。

仅这些完整短语进入快捷分支；带业务正文、预设、参数或字面 `--` 的请求继续原流程。

仅在用户显式点名 AS 时执行。Codex 使用 `$as`；Claude Code 使用 `/as`；WorkBuddy 使用技能选择器明确选中 `as`。共享正文不把自然语言“新开会话”当作触发。默认新会话只接收本次正文；带 `-r` 时同时交付当前会话原始记录的准确路径和固定读取范围，由接收方读取上下文。

## 检查更新

先执行随包 `scripts/check_update.ps1`（Windows PowerShell）或 `scripts/check_update.sh`（macOS/Bash）。0 继续，不复述例行输出；10 原样转述更新报告并询问是否拉取，明确同意后才执行 `-Pull` 或 `--pull`。未回应或检查失败均使用当前版本继续。复制安装按 [更新协议](references/update.md) 查询固定 tag/commit。关闭检查只修改用户目录 `.config/as/.env` 的 `AUTO_UPDATE_CHECK=0`。

## 定位与配置

入口为本 Skill 实际目录下的 `scripts/as.py`。保持调用时的工作目录，不 `cd` 到安装目录。所有命令使用 `uv run --frozen --project <Skill目录> python <Skill目录>/scripts/as.py ...`；必要运行代码随包分发。命令参数、模型目录、错误与恢复详见 [运行入口](references/runtime.md)。

按原始调用入口决定执行流程：

| 输入 | 当前会话执行的流程 |
| --- | --- |
| 除上述纯配置快捷短语外，以 `$as` 或 `/as` 开始；WorkBuddy 明确选中本技能后提供调用内容 | 执行下文“启动一次”，将正文交给新会话。包括 `$as 打开as的设置页面` 这类正文提到 AS 配置的调用。当前会话只负责启动与回执，不执行正文任务 |
| 没有上述命令前缀，明确要求管理 AS，例如“请用 as 打开当前项目的配置页” | 在当前会话执行 `ui start`，交付返回链接 |
| 启动入口返回 `configuration_required` | 按下文“补齐默认配置”展示推荐组合与保存范围，沿用原请求接续 |

页面在两类配置都为空且提示未处理时展示“添加常用配置”。添加只准备草稿，保存才写入；添加、关闭或发现已有任意配置后，提示持久结束。用户可独立编辑和删除默认值与预设。配置独立保存在用户目录 `.config/as/config.json`，显式调用时自动读取；不读取其他 Skill 的设置。UI 的保存成功只代表本地配置与模型目录校验通过。

首词命中已保存预设时使用该预设，其余文字使用当前实际 Agent 的默认值并作为正文。`--preset <代码>` 明确选择预设，缺失或未知代码报错；`--` 结束参数解析，后续文字均为正文：`$as -- <正文>` 使用默认值，`$as a -- <正文>` 保留预设 a。适用于正文以预设代码或保留参数开头。解析交给脚本；不带 `-r` 且正文为空时打开空白会话，示例与空白保留规则见[运行入口](references/runtime.md)。当前 Agent 从本会话身份确定，传 `codex`、`claude` 或 `workbuddy`，不能根据预设倒推。当前实现可启动 Codex、Claude Code；WorkBuddy 调用者使用显式指定目标的预设，无匹配默认 Agent 时返回缺项，不冒充其他 Agent。

`-r` 可放在预设前后，例如 `$as a -r`、`$as -r a`、`$as -r`；正文开始后不再解析参数。`$as a -- -r` 将 `-r` 作为普通正文。没有附加正文时，新会话读取上下文后确认接收并等待；有正文时先读取再处理。配置页只预览此行为，不读取真实会话记录。

Claude Code 本地技能入口提供的当前原生会话 ID：`${CLAUDE_SESSION_ID}`。仅当当前来源确实为 Claude Code 且占位符已被宿主替换时，将此值传给 `--source-session-id`；仍是字面占位符时按来源缺项处理。Codex 使用自己的进程会话 ID，不读取此占位符。

## 启动一次

未知候选代码后紧接 `-r` 时，脚本返回 `unknown_preset`，即使默认配置也缺失，仍先修正预设错误。`$as wrong 正文 -r` 的正文已开始，保留末尾 `-r`；要发送字面 `wrong -r` 使用 `$as -- wrong -r`。原始文本完整交给脚本，不由 Agent 判断意图或猜测近似预设。

1. 将原始调用文本写到当前项目已忽略的 `tmp/as/<本次调用>/input.txt`，UTF-8 编码。WorkBuddy 已显式选中本技能且调用内容没有命令前缀时，仅补齐 `$as ` 前缀，当前 Agent 仍传 `workbuddy`。保留原文中的空格、换行和字面字符，不补写预设代码或改写正文。带 `-r` 时先按[原始记录交接](references/context.md)取得当前来源身份；不生成摘要、不把整份历史粘进正文。
2. 为此次用户调用生成一个唯一 `request-id`，后续复查和恢复沿用。执行 `launch --current-agent <当前Agent> --input-file <原文文件> --request-id <固定ID>`，来源身份的额外参数按上述引用传递。脚本读取真实配置和模型目录，核对来源文件（仅 `-r`）、Orca 精确工作区地址、路径和分支，然后只调用一次 `orca agent launch`。
3. 只以回执判断结果：`complete` 表示收到会话 handle，有正文或 `-r` 时还确认 `journaled` 或 `handed-to-terminal`。说明目标 Agent、模型、强度、目录及是否交付上下文读取指令；消息交付不代表接收方已经读完记录或正文任务已经完成。权限和 Chat/CLI 界面模式沿用 Orca 启动配置。
4. `unknown`、`unconfirmed`、超时或输出丢失时，执行同一 ID 的 `inspect`。有原生 operationId 才用 `resume` 重放同一操作；没有 ID 则通过 Orca 只读核对工作区，保持结果未确定。不能换 ID 再启动、另开终端或补发正文。业务完成后可移除本次原文临时文件；恢复记录含原文，保留在私有运行目录直至用户要求清理。

调用位于 Orca 工作区根目录之外、Orca 不运行、目标 Agent 缺失或模型目录不可用时，报告具体原因并引导修复。脚本不会创建分支、切换目录、注册新工作区或选择另一个 Orca 实例来掩盖失败。

## 补齐默认配置

`launch` 返回 `status=configuration_required`、退出码 1 时尚未提交 Orca。向用户展示回执 `configuration` 中的目标 Agent、推荐模型、思考强度、准确保存路径及不可用原因，提供“采用推荐值并保存后继续”和“打开配置页自行设置”。WorkBuddy 没有推荐默认值，按回执引导选择明确目标的预设。

- 用户明确选择快捷采用后，在原 `launch` 命令上增加 `--adopt-recommended-default`；输入文件、当前宿主、工作目录、配置路径、来源会话参数和 request-id 全部保持原样。脚本只补当前 Agent 默认值，检查原修订与当前目录，成功回读后继续启动。
- 用户选择配置页时，以相同配置路径打开 `ui start`。保存后回读 `config`，再执行原 `launch` 命令（不加采用标记）。页面保存本身不创建会话。
- 保存冲突、推荐组合不可用或回读失败时停止并报告具体问题。配置被其他操作填写后，先回读、核对用户选择，再用原调用接续；不再次快捷覆盖。`unknown` / `unconfirmed` / `inflight` 仍按原 ID 检查与恢复，不进入缺项修复重发流程。

## 配置页链接

页面默认空闲 60 分钟到期；点击、输入、滚动续期。交付采用本次回执的实际剩余期限，复用不重算。`ui status` 与 `ui stop` 管理同一安装、项目和配置目标。首次附一条填好实际项目绝对路径的通用重开请求：“请打开项目【实际路径】中 as 的配置页，给我新的访问链接。”同会话重发同页面只给链接与实际期限。

首次链接附近只附：“手机访问：告诉 Agent‘我要用手机打开这个页面。’”默认使用本机地址；用户要求后才配置通道并验证准确 external origin。
