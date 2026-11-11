# 清洗与配置

清洗是按用途删改内容，独立于忠实转换。只提出当前任务要求时，保存本轮快照；明确要求长期保存或修改默认才写项目文件。操作前说明将改变哪些内容及范围，沿用已有选择。没有选定方案保留原文。

## 读取与来源

唯一业务字段 `PDF_TO_MARKDOWN_CLEANING_PROFILE` 保存方案路径。相对路径一律相对已选项目根，不向父目录查找，不扫描方案目录，不读其他 Skill 的配置。

按首个非空值读取：进程环境 → `<project>/.env.pdf-to-markdown-vision` → `<project>/.env.local` → `<project>/.env`。仅显式 `--use-global-config` 时追加 `Path.home()/.config/pdf-to-markdown-vision/.env`。配置文件存在但不可读时报告错误；缺失与空值继续回退。每个文件同名字段取最后一次；支持普通或单双引号值，空白和整行注释；不执行变量展开、命令替换、续行或行尾注释。

用 `config --project <根>` 检查真实生效值及来源。`AUTO_UPDATE_CHECK` 是独立的更新开关，始终只从个人 `.config/pdf-to-markdown-vision/.env` 读取，不走业务优先级。

当前调用覆盖：`--cleaning <方案路径>` 选其他方案；`--instructions <本次要求.md>` 在未另选方案时取代默认，有显式方案时作为优先覆盖；`--no-cleaning` 明确不清洗。不能同时要求“不清洗”和临时清洗。

## 长期方案与默认

通过对话明确用途、处理对象、匹配条件、保留例外和修改版本，写成 Markdown。匹配条件必须能定位内容，不能笼统删除全部页底文字。技术条件、单位、安全提示和脚注属于保留例外，除非用户明确要求删改。

明确长期保存后，用 `profile-save --project <根> --name <可读短名> --input <方案.md>` 写到 `config/pdf-to-markdown/cleaning/<name>.md` 并读回。明确设默认时追加 `--set-default`，或 `default-profile --project <根> --profile <方案路径>`。默认只写项目 `.env.local`，保留无关配置，优先保存相对路径。报告准确保存路径与 `effective.source`；若环境变量或 Skill 专用文件遮蔽它，如实说明尚未生效，不修改高优先级来源。

长期文件位于项目，Skill 更新不覆盖它。修改方案不批量改写其他任务。

## 执行

每轮快照保存实际文本、来源和临时覆盖。程序能检查存在性和文件完整，语义是否明确由 Agent 判断。选定方案缺失、不可读、空白或不足以确定动作时，停止依赖该方案的清洗；保留未清洗结果。语义不明确时 `block-cleaning --task ... --writer ... --reason ...`，然后 `finish` 交付明确标注的阶段结果，不能声称选定清洗已经完成。

`finish` 返回 `needs-cleaning` 时，从它的 `input_dir/raw.md` 生成最终 Markdown，沿用现有相对图片链接。把以下 JSON 保存到任务证据目录并调用 `submit-cleaning --task ... --writer ... --input <文件>`，然后再 `finish`：

```json
{
  "markdown": "完整清洗后正文，保持仍需要的图片引用。",
  "review": "已将删除项逐一与选定条件比较，保留了单位、脚注和安全提示。",
  "changes": [
    {"page": 1, "block": "header", "action": "remove", "reason": "该区域符合本轮方案中的重复品牌页眉条件。"}
  ]
}
```

`page` 为物理页、`block` 为该页区域 ID。没有匹配项时 `changes: []` 并写实际核验结论，不能为了体现清洗而删内容。清洗后逐项核对：删改均有范围依据，例外仍在，原始结果未覆盖，图文关系、技术参数和公式含义未意外改变。
