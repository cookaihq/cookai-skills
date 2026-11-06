---
name: skill-configuration-and-artifacts-ui
description: v0.2.0｜为新建或已有 Skill / Plugin 设计、接入和改造配置与产物 UI；评估必要性、确认展示方案，并提供配置中心、产物集合与专注预览的共用规则和代码。适用于 Skill 开发及界面改造。
metadata:
  version: "0.2.0"
---

# Skill 配置与产物 UI

面向开发者的新建接入与存量改造工具。处理用户指定的 Skill / Plugin，业务用户使用的 UI 运行代码随目标产物分发。本文路径相对于本 Skill 的实际安装目录；目标路径相对于用户明确的目标项目。

## 0. 检查更新

每次调用先执行 `scripts/check_update.ps1`（Windows 原生 PowerShell）或 `scripts/check_update.sh`（macOS / Bash）。退出码 0 继续，不复述例行输出；10 原样展示更新报告并询问是否拉取，同意才用同一脚本的 `--pull`（PowerShell 为 `-Pull`）。用户未回应、检查或更新失败均按当前版本继续。复制安装按 [更新协议](references/update.md) 查询固定 tag/commit。用户要求关闭时只修改用户主目录 `.config/skill-configuration-and-artifacts-ui/.env` 的 `AUTO_UPDATE_CHECK=0`。

## 1. 调查并决定适用性

读取 [调查与改造](references/assessment.md)；涉及配置时，在形成方案前同时读取 [配置规则](references/configuration.md)，按其中的配置方式边界调查存量入口。查目标源码、已有 UI、配置加载与保存、输出与任务记录、发布包和宿主入口。分别给配置 UI、产物 UI 的必要性结论及证据，让用户决定两者、其一或均不需要；沿用仍适用的决定。此开发 Skill 本身不需要业务配置或产物服务，不递归给自己接入 UI。

初次需求、后续追加调整或预览反馈出现复杂且影响方案的关键缺口时，按 [复杂需求澄清](references/assessment.md#复杂需求澄清) 主动推荐 `mattpocock-skills:grill-with-docs`，沿用用户选择后接回当前阶段。

完成条件：目标和项目明确，每项配置/产物可对应实际读写或记录入口；新增数据采集与存量 UI 的处置已经确定。仅评估请求到此交付方案，不擅自改造。

## 2. 确认展示与预览

涉及产物时读取 [产物规则](references/artifacts.md)；启动任意页面前读取 [生命周期与访问](references/access-and-lifetime.md)。按调查结果列出具体展示方案和待选项。新界面或明显变化先做可运行的合成数据预览，用户确认后真实接入。已确认且仍适用的界面无需再选一次。

## 3. 接入或改造

读取 [代码接入](references/integration.md)。`assets/runtime/skill_ui/` 是共用运行代码，`examples/studio_adapter.py` 是可运行适配示例。用 `scripts/vendor.py` 先检查差异，再把固定版本核心带入目标；业务适配与定制文件放在核心之外。Plugin 共用一份核心。使用目标已有的配置和数据协议，不能用示例 JSON 文件替换原加载器。

改造保留仍受支持的原命令与业务行为；已退出的配置方式按 [配置规则](references/configuration.md#配置方式与存量改造) 清理入口与调用方，不能因通用保留要求恢复。其他语义变化先提出具体方案，已有明确决定直接落实。共用包升级先核对版本、校验值和本地改动；冲突解决前不覆盖。使用者不依赖安装本开发 Skill。

## 4. 验收与交付

读取 [验收](references/acceptance.md)，沿用用户已选的实际验收 Agent；没有决定才询问，不按当前执行 Agent 推定。完成原业务入口、UI、发布副本与所选宿主的实际验证，报告已测范围及缺口。

交付 UI 链接时按访问规则给出醒目的 **📱 FRP / Tailscale** 跨设备提示。保存成功只代表配置保存及所声明校验通过；由仍在等待的 Agent 核对任务关联后接续授权任务，会话结束则提供继续请求。规则正文、代码与引用检查不能替代业务或宿主实测。
