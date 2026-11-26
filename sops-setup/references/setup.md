## SOPS 与 age 操作指引

### 检查已有文件

- `.sops.yaml` 的首条匹配规则用于创建密文或运行 `updatekeys`。解密现有文件依靠文件内的接收者和完整性信息。
- 用 `sops filestatus secrets/dev.env` 判断该路径是否包含 SOPS 加密元数据。输出 `"encrypted": true` 后，再按请求检查能否完成解密。路径和格式以项目为准，无法按后缀识别时显式传 `--input-type`。
- 已有包装命令时，按项目说明选择只验证访问的入口。例如该包装支持 `exec` 时，`pnpm secrets exec dev -- node -e "process.exit(0)"` 可以在 Node 可用的环境中探测开发环境。先确认包装命令本身不会打印明文。为应用注入密钥时继续使用项目的既有部署入口。

原生 PowerShell 的静默解密与退出码检查如下。

```powershell
sops decrypt 'secrets/dev.env' > $null
$decryptExitCode = $LASTEXITCODE
```

Bash 下可以用条件直接判定退出码。

```bash
if sops decrypt secrets/dev.env > /dev/null; then
  printf '%s\n' 'dev decryption OK'
else
  printf '%s\n' 'dev decryption failed' >&2
fi
```

排障时报告可验证的来源。对密钥文件只检查路径、可读性或用 `age-keygen -y` 推导公钥。SOPS 可能同时尝试默认文件、`SOPS_AGE_KEY_FILE`、`SOPS_AGE_KEY` 和已配置的 `SOPS_AGE_KEY_CMD`。命令返回的解密成功并不会指出选中了哪一项。若要证明某个身份独立有效，在隔离进程或目标运行环境中仅提供该身份，并确认没有其他可用接收者的身份。检查生产隔离时，给该进程提供有效的开发身份，对同一生产文件分别验证生产身份成功、开发身份失败。

| 原生 SOPS 运行环境 | 默认 age 身份文件 |
| --- | --- |
| Windows | `%APPDATA%\sops\age\keys.txt` |
| macOS | `~/Library/Application Support/sops/age/keys.txt` |
| Linux 或 WSL | `~/.config/sops/age/keys.txt` |

macOS、Linux 和 WSL 设置了非空 `XDG_CONFIG_HOME` 时改用该目录下的 `sops/age/keys.txt`。WSL 使用发行版内的用户配置。确认当前运行的是原生 SOPS 还是 WSL 内的 SOPS，再给出实际位置。以项目已有来源为先。新建身份的用途和具体保存目标由任务决定。

### 初始化仓库

1. 查清 `.sops.yaml`、目标文件、Git 忽略规则和任何已有密钥入口。工具缺失时从官方说明选择对应安装方式。Windows 选择当前 CPU 架构的发布包，macOS 可用 Homebrew，Linux 和 WSL 选发行版包或官方二进制。安装后运行版本和所需子命令检查。[SOPS 安装](https://getsops.io/docs/installation/) · [age 发布](https://github.com/FiloSottile/age/releases)
2. 优先复用已选定的 age 公钥。需生成新身份时，在已明确的仓外安全位置为每个环境执行 `age-keygen -o <新身份文件>`，实际传入已确定且不存在的绝对路径。POSIX 下确认私钥文件仅所属用户可读写，Windows 使用当前用户可控的私有位置并核对文件实际权限。公钥可用 `age-keygen -y <身份文件>` 取得。生产身份按项目授权放入该项目的部署 Secret 或获准的维护环境。
3. 在仓库根为对应环境写入公开接收者。将下例中两个示例公钥替换成所选身份的实际公钥。已有规则按原来的范围及顺序合并。窄规则放在通用规则之前。Windows 和 POSIX 路径分隔符都能匹配。

```yaml
creation_rules:
  - path_regex: '^secrets[/\\]dev\.env$'
    age: 'age1DEV_RECIPIENT'
  - path_regex: '^secrets[/\\]prod\.env$'
    age: 'age1PROD_RECIPIENT'
```

4. 先用无敏感值验证。以下命令在测试项目根目录运行，`secrets/` 已存在，输出文件尚未创建。Windows 用 `Write-Output 'SOPS_SETUP_CHECK=example'` 作为管道输入。

```bash
printf '%s\n' 'SOPS_SETUP_CHECK=example' |
  sops --config .sops.yaml encrypt --input-type dotenv --output-type dotenv \
    --filename-override secrets/dev.env --output secrets/dev.env
```

显式的 `--filename-override` 用目标路径选中规则。按同样方式验证另一个环境。使用真实配置时，通过可信的输入文件或 stdin 将值交给工具，先生成密文并核验，再写入新目标。写入时检查目标是否已存在。出现并发创建或已有密文时改用项目的更新入口。已有明文文件则先核对迁移和 Git 历史，再按已授权范围处理。

5. 用 `filestatus`、静默解密和开发身份解密生产失败的结果验证实际配置。核对 Git 将纳入的内容，待跟踪内容仅包含公开规则和预期密文。真实业务值、私钥和导出的解密文件保存在已授权的私有来源。

### 更新接收者与轮换

1. 列出受影响文件、它们实际使用的接收者和拟修改的规则。确认对每个文件有仍可用的解密身份。保存原规则及密文副本以便恢复，并记录原有未提交改动。
2. 调整 `.sops.yaml` 后对匹配文件运行 `sops --config .sops.yaml updatekeys secrets/dev.env`。当前任务已明确授权该接收者变更时可使用 `--yes`。新增身份先验证可用，再移除旧身份。若步骤中断，按每个文件的实际接收者核对后接续。
3. `updatekeys` 更新数据密钥的接收者包装。如需重建数据密钥，在移除旧接收者后，以仍有权限的身份运行 `sops rotate --in-place secrets/dev.env`，并再次做静默解密。若需验证撤权，在隔离进程仅提供已撤销的身份测试当前密文。
4. 如果旧私钥已泄漏，轮换它曾有权限解开的业务凭证。已有 Git 历史中的密文仍可被旧私钥解开。

流程参考了独立的密钥作用域和项目包装命令的做法。参数与技术说明以 SOPS 和 age 为准。[using-sops](https://github.com/jssblck/agents/tree/e1294cd9041643344c849e88d7fa5a2342f11a3b/skills/using-sops) · [SOPS 文档](https://getsops.io/docs/) · [age 文档](https://age-encryption.org/)
