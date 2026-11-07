# 可接入现有全局 Skill 开发场景的引用

新建或迁入 Skill / Plugin、设计或修改其配置与产物展示，或用户要求为已有 Skill / Plugin 接入、改造这些界面时，在确定展示方案与编写实现前，调用并读取 `skill-configuration-and-artifacts-ui` 的 `SKILL.md` 及其按条件引用的包内规则。依据实际业务分别判断 UI 必要性、页面组织和模板适用性，沿用用户已确认选择；可以保留已有界面、采用统一界面、多个页面或完全定制。

从当前宿主的可用 Skill 清单解析安装目录。使用标准用户级安装时，Codex 入口相对当前设备用户主目录为 `.agents/skills/skill-configuration-and-artifacts-ui/SKILL.md`，Claude Code 为 `.claude/skills/skill-configuration-and-artifacts-ui/SKILL.md`；WorkBuddy 按本机原生安装入口定位。未安装先报告缺失并提供完整安装方案，不能将正文复制进全局规则。此段是引用模板，实际安装须经用户已有或本次授权，并从主指令建立按需读取链。
