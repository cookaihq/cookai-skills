from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class Word:
    text: str
    start: "float | None"
    end: "float | None"
    speaker: "str | None" = None


@dataclass
class Segment:
    text: str
    start: "float | None"
    end: "float | None"
    speaker: "str | None" = None


@dataclass
class NormalizedResult:
    text: str
    language: "str | None"
    language_probability: "float | None"
    words: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    speakers_present: bool = False
    has_timestamps: bool = False


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _finalize(text, language, prob, words, segments):
    speakers_present = any(getattr(w, "speaker", None) for w in words) or \
                       any(getattr(s, "speaker", None) for s in segments)
    has_ts = any(w.start is not None and w.end is not None for w in words) or \
             any(s.start is not None and s.end is not None for s in segments)
    return NormalizedResult(text or "", language, prob, words, segments, speakers_present, has_ts)


def adapt_scribe(r: dict) -> NormalizedResult:
    """Scribe V2 results[] schema (spec §3, official get-task-detail):
    {text, language_code, language_probability, words:[{text,start,end,type,...}]}
    Word times are in SECONDS. speaker key is upstream-dependent (speaker_id|speaker)."""
    words = []
    for w in (r.get("words") or []):
        words.append(Word(w.get("text", ""), _f(w.get("start")), _f(w.get("end")),
                          (w.get("speaker_id") or w.get("speaker"))))
    return _finalize(r.get("text"), r.get("language_code"), r.get("language_probability"), words, [])


def adapt_cohere(r: dict) -> NormalizedResult:
    """Cohere Transcribe results[] schema (Phase 0 captured, tests/fixtures/cohere.json):
    {text, timings:{download,inference}} — NO word/segment timing (timings are perf
    metrics, not an audio timeline). So words=[], segments=[], has_timestamps=False."""
    return _finalize(r.get("text"), r.get("language"), r.get("language_probability"), [], [])


def adapt_paraformer(r: dict) -> NormalizedResult:
    # BLOCKED: paraformer-v2/-8k-v2 are unprovisioned on foxapi (HTTP 422
    # pricing_not_configured), so Phase 0 could not capture the real results[]
    # schema. Implementing this without a real fixture would be fabrication.
    # See audio-transcribe/PHASE0-BLOCKER.md. Implement once Phase 0 captures it.
    raise NotImplementedError(
        "paraformer adapter pending Phase 0 schema capture (model unavailable on foxapi)")


def adapt(model: str, r: dict) -> NormalizedResult:
    if model.startswith("paraformer"):
        return adapt_paraformer(r)
    if model == "scribe-v2":
        return adapt_scribe(r)
    if model == "cohere-transcribe":
        return adapt_cohere(r)
    raise ValueError("unknown model: " + model)
