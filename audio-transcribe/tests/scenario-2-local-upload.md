# Scenario 2 — 本地文件上传链路

**输入**：用户给出本地文件 `./meeting.mp3`，要英文转写。

**期望 agent 行为**：
1. 先上传（兄弟 skill）：
   ```bash
   URL=$(python3 ../upload-for-url/scripts/upload.py --file ./meeting.mp3)
   ```
   向用户复述「源音频上传链接 72h 后过期」。
2. 用返回 URL + 原文件名 stem 转写：
   ```bash
   python3 scripts/transcribe.py --model cohere-transcribe --audio-url "$URL" \
     --audio-label meeting --language en
   ```
3. **验收**：`find <output>` 显示任务文件夹 `…-meeting/`，内含 `result.json` + 非空 `transcript.txt`；`transcribe.py` 退出码 0。

> cohere 无逐词时间戳 → `.srt/.vtt` 会被跳过并在 stderr 提示，agent 应转达用户。
