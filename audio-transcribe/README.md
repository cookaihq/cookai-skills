# audio-transcribe

foxapi 录音文件识别（STT）skill：封装 `paraformer-v2` / `paraformer-8k-v2` / `scribe-v2` / `cohere-transcribe` 四个模型，自动匹配需求、异步创建任务、轮询到终态，并把结果按规范写成 `result.json` / `transcript.txt` / `.md` / `.srt` / `.vtt`。运行时仅用 Python 标准库。

> 平台可用性（2026-06-18）：foxapi 当前仅 `cohere-transcribe` 可用；scribe/paraformer 暂不可用（见 `PHASE0-BLOCKER.md`）。

## 快速开始

```bash
# 公网 URL 直接转写（cohere）
python3 scripts/transcribe.py --model cohere-transcribe --audio-url "https://.../a.mp3" --language zh

# scribe（自动语种 + 说话人分离 + 音频事件），生成字幕
python3 scripts/transcribe.py --model scribe-v2 --audio-url "https://.../a.mp3" --formats srt,vtt,md

# paraformer 批量（≤100 个 URL），中文 + 说话人分离
python3 scripts/transcribe.py --model paraformer-v2 \
  --audio-url "https://.../1.mp3" --audio-url "https://.../2.mp3" \
  --language-hint zh --diarize --speaker-count 2

# 本地文件：先用兄弟 skill 上传，再转写
URL=$(python3 ../upload-for-url/scripts/upload.py --file ./meeting.mp3)
python3 scripts/transcribe.py --model cohere-transcribe --audio-url "$URL" --audio-label meeting
```

Key 通过 `X_API_KEY`（进程 env / `$PWD/.env(.local)` / `--use-local-key` 读 `~/.config/audio-transcribe/.env`）。详见 [SKILL.md](SKILL.md) 与 [references/api-guide.md](references/api-guide.md)。
