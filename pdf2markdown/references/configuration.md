# 配置声明与诊断

在首次配置、缺项、配置错误或检查来源时读取本文件。正常业务直接使用随包加载器，不要求安装 secret-book。

| Key | 必填 | 用途与默认值 |
| --- | --- | --- |
| `AIHUB_API_KEY` | 在线转换必填；已有 ZIP 时无需 | AIHub Bearer Key，敏感，无默认值；上传与任务查询共用同一个来源和指纹 |
| `PDF2MARKDOWN_OUTPUT_DIR` | 否 | 新任务输出根目录；默认调用项目的 `pdf2markdown-output/`；`--output-dir` 优先 |

服务固定为 `https://api.aihubmax.com`，模型固定为 `doc2x-v3`，不存在可拼配到其他服务的 base URL 或模型配置。两项业务变量分别按进程环境、`$PWD/.env.pdf2markdown`、`.env.local`、`.env`、显式 `--use-local-key` 启用的 `~/.config/pdf2markdown/.env` 取首个非空值。已创建任务续跑绑定原来源，不随新优先级改用其他账号。不会读取或迁移 1.x 的 `settings.json`。

缺项或有依据的错误时，沿用用户的凭证管理方式；否则提供 secret-book / 本机手填选择。首次新增默认个人全局，实际写入仍须用户授权。修复写回实际来源；进程环境错误先找启动/注入位置，不能另写全局覆盖。不要回显 Key、查询参数或临时链接。配置保存不授权重新提交结果不明的任务。

选择 secret-book 时，按其随包流程声明上述字段、选择记录、确认映射和本机保存位置；不在本 Skill 复制飞书表操作。手填分支不依赖 secret-book。项目 Secret 保存前核实未跟踪且已忽略；正常任务不保存密钥值。

用真实加载器生成本机来源报告（在用户的调用项目中执行，`<skill>` 替换成实际安装目录）：

```bash
uv run --frozen --project <skill> python -c 'import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); import config; print(json.dumps(config.configuration_report(cwd=Path.cwd(),use_local_key="--use-local-key" in sys.argv[2:]),ensure_ascii=True))' <skill>/scripts [--use-local-key]
```

方括号表示可选参数；未获全局读取选择时省略。该入口只输出是否配置、来源、错误类别及 `online_auth_checked=false`，不发在线鉴权或计费请求。业务验证必须从 `workflow.py run/resume` 的实际结果判断；不要用创建转换任务来单独“测试 Key”。
