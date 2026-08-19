---
name: exit-ip
version: 1.1.0
description: v1.1.0｜Use when the user wants to know the outbound / exit / public IP of the environment that runs this agent and the Claude Agent SDK — phrases like "我的出口IP是什么"、"当前出口IP"、"Claude SDK 用的是哪个IP出网"、"我的公网IP / 外网IP / 外网出口"、"看看我现在的IP归属地 / 运营商"、"whats my ip"、"check my public / egress ip"、"ipinfo". The skill fetches https://ipinfo.io/json directly from the running environment and shows the raw result (ip / city / region / country / org). Do NOT use to geolocate an IP the user pastes in, to inspect a private/LAN address, or to debug a remote host's networking — this only reports THIS environment's own egress.
---

# exit-ip

## Overview

显示**当前 Agent 运行环境所使用的公网出口 IP** 及其归属信息（城市 / 地区 / 国家 / 运营商）。

原理：Agent 与 Claude Agent SDK 在**同一环境内出网**，二者的对外出口是同一个。因此只要**从本环境直接请求** [ipinfo.io/json](https://ipinfo.io/json)，返回的 `ip` 就是该环境对外可见的出口 IP —— 也就是调用 Claude SDK 时对外呈现的出口 IP。

> 说明：本 skill 报告的是**执行本 Agent 的这台环境的出网 IP**。当 SDK 请求与本 skill 的请求走同一网络路径时（本地机器 / 同一容器的常见情形），它就等于 Claude SDK 的出口 IP。

## When to Use

- 用户想知道**自己当前的公网 / 出口 IP**（"我的IP是多少"、"出口IP"、"外网IP"）
- 用户想确认 **Claude SDK / Agent 出网走的是哪个 IP**、归属哪个地区 / 运营商
- 用户想看 IP 的地理归属（city / region / country）或所属机构（org / ISP）

## When NOT to Use

- 用户给出一个 **IP 让你查归属**（那是对任意 IP 的地理定位，不是"本机出口"）
- 查内网 / 局域网地址（`192.168.*`、`10.*`、`127.0.0.1` 等）
- 排查**远端主机**的网络问题（本 skill 只反映**本 Agent 环境**的出网）

## 第 0 步：检查更新（每次运行都做）

先在本 skill 目录下运行：

```bash
scripts/check_update.sh
```

- 退出码 `0`：直接进入下一步，**不要**向用户复述脚本输出。
- 退出码 `10`：把脚本打印的报告**原样转述给用户**，并询问是否现在拉取。
  - 用户同意 → 运行 `scripts/check_update.sh --pull`，成功后按新版本继续；失败时把脚本给出的拒绝原因转述给用户，然后**按当前版本继续本次任务**，不要卡在更新上。
  - 用户拒绝或不理会 → 按当前版本继续，本次任务内不再提更新。
- 用户说「关掉自动检查更新」→ 往 `~/.config/exit-ip/.env` 写 `AUTO_UPDATE_CHECK=0`（该文件已存在则只改 / 追加这一行，不动其他行）。

**更新检查永远不是任务的阻塞项**：检查失败、拉取失败、用户不理会，一律落到「按当前版本继续干活」。

## How

**直接从本环境请求 ipinfo.io，拿未经改写的原始 JSON**（最能真实反映出口，避免经过第三方渲染 / 摘要的中转）：

```bash
curl -fsS --max-time 10 https://ipinfo.io/json
```

- `-f`：HTTP 错误码时返回非 0，便于判断失败；`-sS`：静默但仍显示错误。
- `--max-time 10`：**必须带**，给这次请求设总时长上限，避免网络异常时命令挂死（元数据查询类，10 秒足够）。
- 若 `curl` 不可用，可退而用 `wget -qO- --timeout=10 https://ipinfo.io/json`（同样必须带超时）。
- **不要**用会经模型改写的网页抓取工具去"总结"结果——出口 IP 必须来自本环境的真实出网请求，且要**原样**呈现。

## Output

把结果**原样**展示给用户，并附一行人类可读摘要。典型返回：

```json
{
  "ip": "203.0.113.42",
  "city": "Singapore",
  "region": "Singapore",
  "country": "SG",
  "loc": "1.2897,103.8501",
  "org": "AS13335 Cloudflare, Inc.",
  "timezone": "Asia/Singapore"
}
```

呈现示例：

> **出口 IP：`203.0.113.42`** ｜ 归属：Singapore, SG ｜ 运营商：AS13335 Cloudflare, Inc.
>
> （以下为 ipinfo.io/json 原始返回）
> ```json
> { ...原样贴出... }
> ```

## Failure Handling

**先分类，再决定是否重试**（这是一次幂等 GET，重试安全）：

- **瞬时失败可重试，至多 3 次尝试**（首次 + 2 次重试）：超时（curl 退出码 28）、连接失败 / 连接重置 / DNS 解析失败（curl 退出码 6/7/35/56 等）、HTTP 5xx、HTTP 429。两次重试前分别等 **2 秒、4 秒**（区间稍宽于通用默认的 1s/2s：ipinfo.io 对匿名调用有限流，退得太快容易连撞）。每次重试都要向用户说明第几次、等了多久、什么原因。
- **确定性失败不重试，直接如实告知**：HTTP 4xx（403 被限、404 等）——重试必然同样失败。
- 3 次尝试后仍失败：**如实告知**本环境当前无法访问 ipinfo.io，附最后一次的错误原因，**不要编造 IP 值**，也不要改用会经模型改写的网页抓取工具去"猜"。
- 返回体里带 `"bogon": true`（内网 / 保留地址）：说明本环境对外并非公网出口（可能在受限网络 / 未直连公网），据实说明而非硬报一个地址。
