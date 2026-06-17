from __future__ import annotations
import json

MODELS = ("paraformer-v2", "paraformer-8k-v2", "scribe-v2", "cohere-transcribe")
PARAFORMER = ("paraformer-v2", "paraformer-8k-v2")
PARAFORMER_LANGS = {"zh", "en", "ja", "yue", "ko", "de", "fr", "ru"}
COHERE_LANGS = {"en","fr","de","it","es","pt","el","nl","pl","zh","ja","ko","vi","ar"}

# Optional params allowed per model (audio is handled separately).
ALLOWED = {
    "paraformer-v2": {"language_hints","diarize","speaker_count","disfluency_removal",
                      "timestamp_alignment","vocabulary_id","special_word_filter","channel_id"},
    "paraformer-8k-v2": {"diarize","speaker_count","disfluency_removal",
                         "timestamp_alignment","vocabulary_id","special_word_filter","channel_id"},
    "scribe-v2": {"language_code","diarize","tag_audio_events","keyterms"},
    "cohere-transcribe": {"language","punctuation","max_new_tokens"},
}
OPTIONAL_KEYS = {"language_hints","language_code","language","diarize","speaker_count",
                 "disfluency_removal","timestamp_alignment","vocabulary_id","special_word_filter",
                 "channel_id","tag_audio_events","keyterms","punctuation","max_new_tokens"}


class ValidationError(Exception):
    pass


def _set(v):
    return v is not None and v != [] and v != ""


def build_payload(p: dict) -> dict:
    """Validate a normalized params dict and return the foxapi request body.
    Raises ValidationError on any misuse. Boolean tri-state: None means 'omit'."""
    model = p.get("model")
    if model not in MODELS:
        raise ValidationError("--model must be one of " + ", ".join(MODELS))
    urls = p.get("audio_urls") or []
    if not urls:
        raise ValidationError("at least one --audio-url is required")
    if model not in PARAFORMER and len(urls) > 1:
        raise ValidationError(model + " accepts exactly one --audio-url")
    if model in PARAFORMER and len(urls) > 100:
        raise ValidationError("paraformer accepts at most 100 --audio-url")
    if len(p.get("audio_labels") or []) > len(urls):
        raise ValidationError("--audio-label count cannot exceed --audio-url count")

    allowed = ALLOWED[model]
    for key in OPTIONAL_KEYS:
        if key not in allowed and _set(p.get(key)):
            raise ValidationError("--" + key.replace("_", "-") + " is not supported by " + model)

    for lg in (p.get("language_hints") or []):
        if lg not in PARAFORMER_LANGS:
            raise ValidationError("language hint '%s' not in %s" % (lg, sorted(PARAFORMER_LANGS)))
    if model == "cohere-transcribe" and _set(p.get("language")) and p["language"] not in COHERE_LANGS:
        raise ValidationError("--language must be one of " + ", ".join(sorted(COHERE_LANGS)))
    sc = p.get("speaker_count")
    if _set(sc):
        if not (2 <= sc <= 100):
            raise ValidationError("--speaker-count must be in 2..100")
        if p.get("diarize") is not True:
            raise ValidationError("--speaker-count requires --diarize")
    kt = p.get("keyterms") or []
    if len(kt) > 100:
        raise ValidationError("--keyterm accepts at most 100 entries")
    for t in kt:
        if len(t) > 50:
            raise ValidationError("each --keyterm must be <= 50 chars")
    mnt = p.get("max_new_tokens")
    if _set(mnt) and mnt < 1:
        raise ValidationError("--max-new-tokens must be >= 1")
    swf = p.get("special_word_filter")
    if _set(swf):
        try:
            json.loads(swf)
        except Exception:
            raise ValidationError("--special-word-filter must be valid JSON")

    body = {"model": model}
    if model in PARAFORMER:
        body["file_urls"] = urls
    else:
        body["audio_url"] = urls[0]

    if model == "paraformer-v2" and p.get("language_hints"):
        body["language_hints"] = p["language_hints"]
    if model in PARAFORMER:
        diar = {}
        if p.get("diarize") is not None:
            diar["enabled"] = p["diarize"]
        if _set(p.get("speaker_count")):
            diar["speaker_count"] = p["speaker_count"]
        if diar:
            body["diarization"] = diar
        rec = {}
        if p.get("disfluency_removal") is not None:
            rec["disfluency_removal_enabled"] = p["disfluency_removal"]
        if p.get("timestamp_alignment") is not None:
            rec["timestamp_alignment_enabled"] = p["timestamp_alignment"]
        if _set(p.get("vocabulary_id")):
            rec["vocabulary_id"] = p["vocabulary_id"]
        if _set(swf):
            rec["special_word_filter"] = swf
        if rec:
            body["recognition"] = rec
        if _set(p.get("channel_id")):
            body["channel_id"] = p["channel_id"]
    elif model == "scribe-v2":
        if _set(p.get("language_code")):
            body["language_code"] = p["language_code"]
        if p.get("diarize") is not None:
            body["diarize"] = p["diarize"]
        if p.get("tag_audio_events") is not None:
            body["tag_audio_events"] = p["tag_audio_events"]
        if p.get("keyterms"):
            body["keyterms"] = p["keyterms"]
    elif model == "cohere-transcribe":
        if _set(p.get("language")):
            body["language"] = p["language"]
        if p.get("punctuation") is not None:
            body["punctuation"] = p["punctuation"]
        if _set(p.get("max_new_tokens")):
            body["max_new_tokens"] = p["max_new_tokens"]
    return body
