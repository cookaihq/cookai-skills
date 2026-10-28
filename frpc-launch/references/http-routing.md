# HTTP / HTTPS 入口与路由核对

已有公网域名、自定义域名或同域名按路径转发时读取本文件。先复用已核实的部署，缺少配置只代表未知，不自动要求重配服务器。这里的域名和端口均为示例，不能作为用户默认值。

## 先按请求流确认 TLS 在哪里结束

| 已核实的请求流 | frpc 代理类型与条件 |
| --- | --- |
| 浏览器 HTTPS → Nginx 解密 → frps `vhostHTTPPort` → frpc → 本地 HTTP | `type=http`；Nginx 传递原始 Host，frps 按域名/路径选择代理 |
| 浏览器 HTTPS → frps `vhostHTTPSPort` → frpc → 本地 TLS 服务或已配置的 TLS 插件 | `type=https`；核对 SNI 域名和证书实际提供方；不能直接转给普通 HTTP 服务 |
| HTTP 客户端 → frps `vhostHTTPPort` → frpc → 本地 HTTP | `type=http`；公网端口来自实际对外监听或端口映射 |

frpc 与 frps 的控制连接使用 `serverAddr:serverPort`；这不是网站 URL。控制连接的 TLS 设置也不决定浏览器侧使用 HTTP 还是 HTTPS。TCP/UDP 使用独立的 `remotePort` 转发，不套用上表的 HTTP Host 路由。

Nginx 位于 frps 前面时，检查已经生效的 `proxy_pass` 是否指向正确 vhost 入口、`proxy_set_header Host $host` 或等效设置是否保留目标域名，以及现有 location 是否重写路径。缺少这类证据才报告具体未知项，不从端口号猜协议。

## 域名相关的四项事实分别核对

1. **DNS**：完整主机名实际解析到哪里；已有泛域名解析不代表所有层都匹配。
2. **TLS**：通过原始 HTTPS URL 正常验证实际证书与完整主机名。证书 `*.example.com` 可匹配 `app.example.com`，不能据此认定匹配 `api.app.example.com` 或裸域 `example.com`。
3. **Nginx**：已有生效的 `server_name` 是否覆盖请求，以及是否存在优先匹配的其他 server。`server_name example.com *.example.com` 可服务对应子域名，不必为每个子域名另建 server；精确域名匹配优先于通配匹配。Nginx 通配匹配规则与证书通配规则不同。
4. **FRP**：frpc 的 `customDomains` 是否是需要访问的域名；使用 `subdomain` 时，核对 frps 已配置的 `subDomainHost`。服务端的域名匹配不会自动为本地 frpc 创建代理。

前置配置已经满足时，继续检查本地目标和代理。业务要求单独的日志、缓存或鉴权策略时，说明该策略限制了哪个业务动作；不能把它说成添加 FRP 路由的必要步骤，也不默认关闭日志、关闭缓存或新增精确 server。

## 记录完整映射，保留已有路径

逐条填写：`公网协议/主机/端口/路径 → 代理名与 type → customDomains/subdomain + locations → localIP:localPort → 预期响应`。同一域名可能把 `/`、API 路径和校验文件交给不同本地端口；不能因为根页面可达就推断全部路径可达，也不能把已有特定路径改成 `/`。

项目流程的 `project-config` 可重复传 `--proxy`，按名称定点合并并保留其他代理；完整原生接口的 `guide-init` 仅用于新建整份配置：

```text
--proxy "name=web;type=http;localIP=127.0.0.1;localPort=8080;customDomains=app.example.com;locations=/"
--proxy "name=api;type=http;localIP=127.0.0.1;localPort=8081;customDomains=app.example.com;locations=/api/"
```

这是同一个前置 HTTPS 域名通过 HTTP vhost 转发到两个本地 HTTP 服务的示例。`locations` 是 HTTP 路径路由，不支持 HTTPS 代理；它不表示去掉路径前缀。本地服务必须能处理实际收到的路径。简写支持范围见[项目配置协议](projects.md)；项目网站快捷入口可用 `--route-mode subdomain --subdomain-host <已核实父域>`。原生插件等高级字段保留在项目 `client` 配置或已有完整原生 TOML 中。

## 逐层定位失败，交付原始入口

| 观察到的证据 | 可得出的结论与下一步 |
| --- | --- |
| 本地指定地址或路径不响应 | 检查对应应用的监听和配置加载；不能先归因于 frps |
| 进程存活，未见登录成功 | 核对连接入口、认证与日志；进程不是成功证据 |
| 已登录，某条代理报告 `start error` | 检查该代理的注册错误；其他代理成功不能代替它 |
| 所有目标代理有成功日志，公网返回错误页面 | 对照域名、路径、TLS、反向代理和应用响应；日志不能证明公网全链路 |
| 公网与本地同路径均返回预期应用 4xx | 可以证明请求已到达该应用；不等于该业务操作成功 |
| 公网 200，但正文是准备页或健康字段未就绪 | 公网转发可用，应用尚未满足业务条件；明确哪项配置/进程仍待处理 |

只读探测设置单次超时和总预算；仅对超时、连接重置、429/5xx 等瞬时错误最多尝试 3 次。普通业务 4xx 不盲重试，有副作用的业务调用不作为随手探针。默认保留 TLS 校验，记录响应状态、类型及必要的脱敏内容特征，不输出 Cookie、token、授权码或完整敏感查询参数。

向用户交付原始完整 URL，并说明验证到哪一层。重写 Host、使用直连 IP、跳过证书校验或调用内部健康端点仅为辅助诊断；不能用它们替代用户指定入口。HTTPS 正常可达也不证明浏览器、微信或其他指定客户端中的完整业务流程成功。

## 机制依据

- [frp 官方客户端示例（v0.70.0）](https://github.com/fatedier/frp/blob/v0.70.0/conf/frpc_full_example.toml)：`http`、`https`、`customDomains`、`localIP` 与仅 HTTP 支持的 `locations`。
- [frp 官方服务端示例（v0.70.0）](https://github.com/fatedier/frp/blob/v0.70.0/conf/frps_full_example.toml)：`bindPort`、vhost 端口与 `subDomainHost`。该引用用于字段语义，不要求降级用户已安装的 frp。
- [Nginx server names](https://nginx.org/en/docs/http/server_names.html)、[Nginx HTTP proxy module](https://nginx.org/en/docs/http/ngx_http_proxy_module.html)：域名匹配顺序与 Host 转发。
- [RFC 9525 §6.3](https://www.rfc-editor.org/rfc/rfc9525.html#section-6.3)：TLS 服务身份与通配证书匹配。
