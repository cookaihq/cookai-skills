# 仓库凭证与按次鉴权

Git 不会自动发现 .credentials/repositories。项目规则指向 Git 使用说明，后续 Agent 读说明、定位当前文件、验证完整目标，再通过本次进程环境配置向 Git 提供凭证。配置 remote 时始终使用无凭证 URL。

## 来源选择

- GitHub：明确的 gh 账号，或该仓库独立 Token 文件；不能用 gh active 代替选择。文件只有 Token 原文。
- CNB：单行 https://cnb:<TOKEN>@cnb.cool/<完整仓库路径>。Token 中 URL 特殊字符先百分号编码。允许 UTF-8 BOM 与一个末尾换行；不接受多行字段、引号或额外文本。Git 用户名固定 cnb。
- environment 是明确选择的 GitHub 环境来源；加载顺序见 [配置](configuration.md)，不会自动回退到它。

管理仓关系须明确且可核验。主检出保管 .credentials/repositories/<完整路径各段的连字符转为 %2D，再用双连字符连接>.github-token 或 .cnb-url，Worktree 不复制凭证。已登记子仓可由管理主检出保管，但每个完整远端仍独立选择；URL 相同不证明两个 clone 共用管理关系。

选择优先级：本次 sources 或参数 → 可选 sources.local.md → CNB 的确定路径。GitHub 没有选择则报告 needs_input。非敏感本机记录保存于管理主检出的 .credentials/repositories/sources.local.md，包含唯一 json fenced array：每项 {url,method,file?,account?}。文件路径相对管理主检出，gh 用 account。项目共同规定写入 Git 使用说明。本机差异有必要记住时，沿已明确选择调用 source-save --input - --apply；没有此需要则不创建文件。

## 准备、验证与安全边界

credential-prepare --url <URL> --auth-method <方式> --apply 准备空文件；本次允许增加本机忽略规则时才加 --allow-ignore。必须证明未跟踪且实际忽略；拒绝软链、硬链、越界和过大文件。Unix 目录 700、文件 600；Windows 由 Agent 核对本机 ACL 为当前用户的私有位置，脚本的普通文件检查不能代替 ACL 验证。脚本不代填 Token。

准备后提供 credentialLink、格式和用途，等待用户回复“已填写”。credential-check 读取当前文件，GitHub 另验证账号；CNB 只验证 Git 读取，不声称取得平台账号身份。API-only 只适用于 GitHub，并如实报告未读取 Git。验证不保存 Token 指纹、不安装 helper、不代表推送或 API 写入权限。

同路径轮换在下一次调用直接验证新值；同一次计划/网络操作中的内容变化拒绝继续。缺文件、账号失效或目标不符时修复原来源，不改用其他账号、其他文件或全局 helper。来源报告仅含无值元信息。

## 传输契约

产品脚本每次使用 get-only helper：按 https 协议、主机和完整仓库路径匹配；store/erase 无动作，认证拒绝不会修改源文件。Token 仅在子进程环境中传递，不进 argv、remote、日志、计划或 Git 配置文件。

Git credential 协议可发送重复的 `capability[]`、`wwwauth[]` 等多值元数据。临时 helper 只收集并核对 `protocol`、`host`、`path`、`username` 这些身份字段，身份字段重复时拒绝；忽略其他字段及其合法重复，不把多值元数据误判成目标冲突。不能因为忽略元数据而放松完整仓库与固定用户名检查。

本次 GIT_CONFIG_COUNT 配置先清空 credential.helper，再选择只读 helper，启用 credential.useHttpPath，关闭 ASKPASS/终端提示和重定向。检查实际 URL 生效的额外鉴权头，冲突时停止；不借用另一个身份。Token 缺失或完整目标不符必须返回 quit=true，阻止回退。继承环境中的 Git trace、配置注入、gh Token 等可能干扰选择的变量由运行器清除。

宿主沙箱可能用受保护的进程配置声明当前用户拥有的仓库可信。清理配置注入时，只保留 `safe.directory` 的具体绝对路径及清空列表的空项，并与本次鉴权配置合并；不保留 `*`、递归通配路径、相对路径或其他注入配置。不修改全局信任，也不以放宽沙箱解决所有权检查。项目 Git 说明中的临时调用方法应遵守同一边界。

项目 Git 说明须保留这一操作原理和本项目具体路径，即使没有安装 Skill，Agent 也能用宿主本轮的短期脚本和进程环境执行。不能把带 Token 的 URL 直接作为 git 参数，不能把完整 Skill 复制到 .git。可参考项目已有满足同一契约的只读 helper；无脚本的项目不为此默认生成长期运行包。
