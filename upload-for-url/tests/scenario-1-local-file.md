# Scenario 1 — 本地文件直传

**目标**：用户给本地文件，skill 用 `--file` 上传并返回 URL + 72h 提醒。

## User Turn
> 帮我把 ./clip.mp4 传上去，给我一个能在线访问的链接。（已 export AIHUB_API_KEY）

## Expected (with skill)
- 调 `python3 scripts/upload.py --file ./clip.mp4`
- 输出那行 URL，并**明确说明 72 小时后过期**

## Anti-pattern (baseline)
- 自己写 curl 拼 multipart、或把 key 明文打印
- 不提过期时间
