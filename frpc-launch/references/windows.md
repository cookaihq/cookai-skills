# Windows 原生客户端与恢复

适用于 Windows 10/11 的 amd64/arm64；Windows 原生、WSL、macOS/Linux 分别使用各自的用户目录和运行时。当前机器及各宿主的真实验收范围见 README。

## 客户端准备

沿用统一 uv 入口。`install --mode official --version <已选版本>` 从 `fatedier/frp` 的对应 release 取得 Windows ZIP 与 SHA-256 清单，只提取该平台的 `frpc.exe`，校验后运行 `--version`，最后原子安装到 `<home>/bin/windows/frpc.exe`。缺省版本使用官方 latest API；遇到限流应报告，不自行改用镜像或其他版本。

`sakura` 从樱花官方客户端清单选择对应 Windows EXE，核对 size/MD5，再记录本机 SHA-256 和版本预检结果，保存为 `frpc-sakura.exe`。它只用于樱花服务，不替代连接普通 FRPS 的官方客户端。官方模式支持 `verify` 离线配置校验；樱花隧道配置与业务鉴权仍依赖其服务。

显式 `FRPC_LAUNCH_FRPC` / `FRPC_LAUNCH_SAKURA_FRPC` 必须指向相应原生 `.exe`。文件存在不代表可执行；错误可能来自架构、权限、防护或客户端格式，按实际原因报告。项目、Skill 和可执行路径允许空格及 Unicode。

## 防护阻断

下载、读取、校验或执行被 Defender 等防护拦截时，停止该客户端分支，报告受阻阶段和可见的错误/检测记录。已取得的官方 SHA-256 仅是期望值；未读取本机文件就不能声明哈希匹配。

不关闭防护、不添加排除、不改名、换目录、换镜像或换客户端绕过；不预设为误报。可核对官方 release 与校验清单，以及 Windows 安全中心保护历史，由用户或发布者按 [Microsoft 文件分析入口](https://www.microsoft.com/en-us/wdsi/filesubmission) 处理。提交样本属于另外的对外操作。防护解除后从原来的安装/校验步骤接续；独立的合成进程测试不代表真实 FRP 已通过。

## 后台进程与文件

后台不显示控制台。先保存 PID、创建时间、可执行路径、完整参数和项目身份，之后才放行客户端启动。Windows Job Object 绑定日志收集进程和它的客户端；收集进程退出时，Windows 回收该 Job 内的子进程。停止持有同一进程句柄核验身份再终止，不使用进程名批量终止。

调用中断后仍使用同一项目及 `--home` 查询 `status/logs`。已退出的记录可清理；PID 文件损坏、活进程身份不符或身份不可读取时拒绝重复启动。当前连接损坏不影响按落盘身份停止。配置变化保留现有进程，按重启授权执行 stop/start。

运行文件和新写入的 Secret 使用限制到当前用户、SYSTEM、Administrators 的受保护 DACL；macOS/Linux 使用 POSIX 私有权限。不能用 Windows `chmod(0700)` 或文件 mode 位证明 ACL 安全。日志写盘前脱敏，单文件达到约 5 MiB 后轮转，仅保留一个 `.log.1`；Windows 并发读者短暂阻止改名时，下一行重试轮转。

不同宿主可能使用不同 Windows 账号。沙箱账号不能自动读取普通用户的受保护文件，也不一定有权给普通用户创建的受管目录设置私有 ACL；错误会带出受影响路径。先核对执行账号、目录所有者与宿主实际授权，按正常授权流程修复。合成验收可由目标账号在独立目录自行创建配置；这不证明它已能读取用户真实配置。不自动向 Everyone、Users 或沙箱组授予 Secret 访问，不更改宿主沙箱策略。
