from __future__ import annotations
import re
from dataclasses import dataclass

MAX_CHARS = 42
MAX_DUR = 5.0
MAX_GAP = 0.8
SENT_END = set("。！？.!?；;")
_UNSAFE = re.compile(r'[\\/:*?"<>|\r\n\t]')


def sanitize_label(raw: str, maxlen: int = 10) -> str:
    if not raw:
        return ""
    s = _UNSAFE.sub("_", raw)
    s = re.sub(r"\s+", "_", s).strip("_")
    return s[:maxlen]


@dataclass
class Cue:
    start: float
    end: float
    text: str
    speaker: "str | None"


def _ends_sentence(t: str) -> bool:
    t = (t or "").rstrip()
    return bool(t) and t[-1] in SENT_END


def _all_words(results):
    out = []
    for r in results:
        out.extend(r.words)
    return out


def segment_into_cues(words) -> list:
    usable = [w for w in words if w.start is not None and w.end is not None]
    cues, cur = [], []
    def flush():
        if not cur:
            return
        text = re.sub(r"\s+", " ", "".join(w.text for w in cur)).strip()
        cues.append(Cue(cur[0].start, cur[-1].end, text, cur[0].speaker))
        cur.clear()
    for w in usable:
        if cur:
            prev = cur[-1]
            chars = len("".join(x.text for x in cur))
            if (w.speaker != cur[0].speaker or (w.start - prev.end) > MAX_GAP
                    or (prev.end - cur[0].start) > MAX_DUR or chars > MAX_CHARS
                    or _ends_sentence(prev.text)):
                flush()
        cur.append(w)
    flush()
    return cues


def _fmt_time(s: float, sep: str) -> str:
    if s < 0:
        s = 0.0
    h = int(s // 3600); m = int((s % 3600) // 60); sec = int(s % 60); ms = int(round((s - int(s)) * 1000))
    if ms == 1000:
        sec += 1; ms = 0
    return "%02d:%02d:%02d%s%03d" % (h, m, sec, sep, ms)


def to_txt(results) -> str:
    parts = [(r.text or "").strip() for r in results]
    return "\n\n".join(p for p in parts if p).strip() + "\n"


def _cues_with_speaker_prefix(cues) -> list:
    out = []
    for c in cues:
        text = ("[说话人 %s] " % c.speaker + c.text) if c.speaker else c.text
        out.append((c.start, c.end, text))
    return out


def to_srt(results):
    cues = segment_into_cues(_all_words(results))
    if not cues:
        return None
    lines = []
    for i, (start, end, text) in enumerate(_cues_with_speaker_prefix(cues), 1):
        lines.append(str(i))
        lines.append("%s --> %s" % (_fmt_time(start, ","), _fmt_time(end, ",")))
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_vtt(results):
    cues = segment_into_cues(_all_words(results))
    if not cues:
        return None
    lines = ["WEBVTT", ""]
    for (start, end, text) in _cues_with_speaker_prefix(cues):
        lines.append("%s --> %s" % (_fmt_time(start, "."), _fmt_time(end, ".")))
        lines.append(text)
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def to_md(model: str, results) -> str:
    words = _all_words(results)
    lang = next((r.language for r in results if r.language), None)
    prob = next((r.language_probability for r in results if r.language_probability is not None), None)
    speakers = any(w.speaker for w in words)
    ends = [w.end for w in words if w.end is not None]
    head = ["# 转写结果", "", "- 模型：`%s`" % model]
    if lang:
        head.append("- 语言：%s%s" % (lang, (" (%.2f)" % prob) if prob is not None else ""))
    if ends:
        head.append("- 时长：%.1fs" % max(ends))
    head.append("- 说话人分离：%s" % ("是" if speakers else "否"))
    head.append("")
    body = []
    if speakers:
        cur_spk, buf = object(), []
        def flush_spk():
            if buf:
                body.append("**说话人 %s：** %s" % (cur_spk, "".join(buf).strip()))
                body.append("")
        for w in words:
            if w.speaker != cur_spk:
                flush_spk(); cur_spk, buf = w.speaker, []
            buf.append(w.text)
        flush_spk()
    else:
        body.append(to_txt(results).strip())
        body.append("")
    return "\n".join(head + body).strip() + "\n"
