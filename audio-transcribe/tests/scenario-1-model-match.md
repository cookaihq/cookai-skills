# Scenario 1 — 模型匹配

**输入**：用户说「这是一段**粤语会议录音**，帮我转文字，要**区分谁在说话**」。

**期望 agent 行为**：
1. 识别维度：语言=粤语(yue) → Paraformer 支持 `yue`；需求=说话人分离。
2. 推荐 **`paraformer-v2`** + `--diarize`，一句理由（粤语方言 + 说话人分离，Paraformer 最合适）。
3. 等用户确认后才调：
   ```bash
   python3 scripts/transcribe.py --model paraformer-v2 --audio-url "<URL>" \
     --language-hint yue --diarize
   ```

**当前平台注意**：paraformer 暂不可用（`pricing_not_configured`）。agent 应推荐正确模型，但在实际创建前提示「该模型目前在 foxapi 不可用，需平台恢复」。
