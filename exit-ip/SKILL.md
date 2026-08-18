---
name: exit-ip
version: 1.0.0
description: v1.0.0｜Use when the user wants to know the outbound / exit / public IP of the environment that runs this agent and the Claude Agent SDK — phrases like "我的出口IP是什么"、"当前出口IP"、"Claude SDK 用的是哪个IP出网"、"我的公网IP / 外网IP / 外网出口"、"看看我现在的IP归属地 / 运营商"、"whats my ip"、"check my public / egress ip"、"ipinfo". The skill fetches https://ipinfo.io/json directly from the running environment and shows the raw result (ip / city / region / country / org). Do NOT use to geolocate an IP the user pastes in, to inspect a private/LAN address, or to debug a remote host's networking — this only reports THIS environment's own egress.
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

## How

**直接从本环境请求 ipinfo.io，拿未经改写的原始 JSON**（最能真实反映出口，避免经过第三方渲染 / 摘要的中转）：

```bash
curl -fsS https://ipinfo.io/json
```

- `-f`：HTTP 错误码时返回非 0，便于判断失败；`-sS`：静默但仍显示错误。
- 若 `curl` 不可用，可退而用 `wget -qO- https://ipinfo.io/json`。
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

- 请求失败（超时 / 非 2xx / 无网络）：**如实告知**本环境当前无法访问 ipinfo.io，不要编造 IP 值。
- 返回体里带 `"bogon": true`（内网 / 保留地址）：说明本环境对外并非公网出口（可能在受限网络 / 未直连公网），据实说明而非硬报一个地址。
