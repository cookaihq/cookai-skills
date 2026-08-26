from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from . import media, state


TIMING = re.compile(
    r"(?P<start>(?:\d+:)?[0-5]?\d:[0-5]?\d(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(?P<end>(?:\d+:)?[0-5]?\d:[0-5]?\d(?:[.,]\d{1,3})?)"
)
PLAIN_TIMING = re.compile(
    r"^\[?(?P<start>(?:\d+:)?[0-5]?\d:[0-5]?\d(?:[.,]\d{1,3})?)\s*-->\s*"
    r"(?P<end>(?:\d+:)?[0-5]?\d:[0-5]?\d(?:[.,]\d{1,3})?)\]?\s+"
    r"(?P<text>.+)$"
)
SENTENCE_END = set(".!?。！？；;")


class TranscriptError(ValueError):
    pass


def _precision_ms(raw: str) -> int:
    fraction = re.search(r"[.,](\d{1,3})$", raw.strip())
    if fraction is None:
        return 1000
    return 10 ** (3 - len(fraction.group(1)))


def _segment(index: int, start_raw: Any, end_raw: Any, text: str, precision: int | None) -> dict:
    start_ms = media.parse_timecode(str(start_raw)) if isinstance(start_raw, str) else start_raw
    end_ms = media.parse_timecode(str(end_raw)) if isinstance(end_raw, str) else end_raw
    return {
        "segment_id": "segment-%06d" % index,
        "raw_start": start_raw,
        "raw_end": end_raw,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "timestamp_precision_ms": precision,
        "text": re.sub(r"\s+", " ", text).strip(),
    }


def parse_subtitle(
    path: Path,
    *,
    language: str | None = None,
    duration_ms: int | None = None,
) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise TranscriptError("cannot read subtitle: %s" % path) from exc
    suffix = path.suffix.lower()
    if suffix == ".vtt":
        kind = "vtt"
        raw = re.sub(r"^WEBVTT[^\n]*\n", "", raw, flags=re.IGNORECASE)
    elif suffix == ".srt":
        kind = "srt"
    else:
        kind = "timestamped_transcript"
    segments = []
    if kind in {"srt", "vtt"}:
        for block in re.split(r"\n\s*\n", raw.strip()):
            lines = [line.strip("\ufeff\r") for line in block.splitlines()]
            timing_index = next((i for i, line in enumerate(lines) if TIMING.search(line)), None)
            if timing_index is None:
                continue
            match = TIMING.search(lines[timing_index])
            assert match is not None
            text = " ".join(lines[timing_index + 1 :]).strip()
            if not text:
                continue
            start_raw = match.group("start")
            end_raw = match.group("end")
            segments.append(
                _segment(
                    len(segments) + 1,
                    start_raw,
                    end_raw,
                    text,
                    max(_precision_ms(start_raw), _precision_ms(end_raw)),
                )
            )
    else:
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            plain = PLAIN_TIMING.match(stripped)
            if plain is None or not plain.group("text").strip():
                raise TranscriptError(
                    "timestamped transcript lines must contain start --> end and text"
                )
            start_raw = plain.group("start")
            end_raw = plain.group("end")
            segments.append(
                _segment(
                    len(segments) + 1,
                    start_raw,
                    end_raw,
                    plain.group("text"),
                    max(_precision_ms(start_raw), _precision_ms(end_raw)),
                )
            )
    if not segments:
        raise TranscriptError("subtitle contains no timestamped text segments")
    _limit_segment_times(segments, duration_ms)
    return {
        "schema_version": 1,
        "source": {"kind": kind, "path": str(path.resolve())},
        "language": language,
        "upstream": {"raw": raw},
        "segments": segments,
    }


def _number_precision_ms(value: Any) -> int | None:
    if value is None:
        return None
    text = repr(value)
    if "e" in text.lower():
        return None
    if "." not in text:
        return 1000
    decimals = len(text.rsplit(".", 1)[1].rstrip("0"))
    return max(1, 10 ** max(0, 3 - min(decimals, 3)))


def _seconds_to_ms(value: Any) -> int | None:
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _limit_segment_times(segments: list[dict], duration_ms: int | None) -> None:
    if duration_ms is None:
        return
    if type(duration_ms) is not int or duration_ms < 0:
        raise TranscriptError("video duration must be a non-negative integer number of milliseconds")
    for index, segment in enumerate(segments, 1):
        segment_id = segment.get("segment_id") or "segment-%06d" % index
        start_ms = segment.get("start_ms")
        end_ms = segment.get("end_ms")
        if start_ms is None and end_ms is None:
            continue
        if type(start_ms) is not int or type(end_ms) is not int:
            raise TranscriptError(
                "%s has incomplete or non-integer normalized timestamps" % segment_id
            )
        adjustments = []
        for field, value in (("start_ms", start_ms), ("end_ms", end_ms)):
            limited = min(max(value, 0), duration_ms)
            if limited != value:
                adjustments.append(
                    {
                        "field": field,
                        "from_ms": value,
                        "to_ms": limited,
                        "reason": "before_video_start"
                        if value < 0
                        else "after_video_end",
                    }
                )
                segment[field] = limited
        if segment["start_ms"] > segment["end_ms"]:
            raise TranscriptError("%s starts after it ends" % segment_id)
        if adjustments:
            segment["timing_adjustments"] = adjustments


def _word_cues(words: list[dict]) -> list[dict]:
    usable = []
    for word in words:
        if not isinstance(word, dict):
            continue
        start_ms = _seconds_to_ms(word.get("start"))
        end_ms = _seconds_to_ms(word.get("end"))
        if start_ms is None or end_ms is None:
            continue
        usable.append(
            {
                "raw": word,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": str(word.get("text") or ""),
                "speaker": word.get("speaker_id") or word.get("speaker"),
                "precision": max(
                    _number_precision_ms(word.get("start")) or 1,
                    _number_precision_ms(word.get("end")) or 1,
                ),
            }
        )
    cues = []
    current = []

    def flush() -> None:
        if not current:
            return
        text = "".join(item["text"] for item in current).strip()
        if text:
            cues.append(
                {
                    "raw_start": current[0]["raw"].get("start"),
                    "raw_end": current[-1]["raw"].get("end"),
                    "start_ms": current[0]["start_ms"],
                    "end_ms": current[-1]["end_ms"],
                    "timestamp_precision_ms": max(item["precision"] for item in current),
                    "text": re.sub(r"\s+", " ", text),
                }
            )
        current.clear()

    for item in usable:
        if current:
            previous = current[-1]
            changed_speaker = item["speaker"] != current[0]["speaker"]
            gap = item["start_ms"] - previous["end_ms"]
            duration = previous["end_ms"] - current[0]["start_ms"]
            chars = sum(len(value["text"]) for value in current)
            ended = bool(previous["text"].rstrip()) and previous["text"].rstrip()[-1] in SENTENCE_END
            if changed_speaker or gap > 800 or duration > 12_000 or chars > 180 or ended:
                flush()
        current.append(item)
    flush()
    return cues


def _redact_urls(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted-temporary-url>"
            if "url" in str(key).lower()
            else _redact_urls(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_urls(item) for item in value]
    return value


def _paraformer_sentences(response: dict | None) -> list[dict]:
    if not isinstance(response, dict):
        return []
    normalized = []
    transcripts = response.get("transcripts")
    if not isinstance(transcripts, list):
        return normalized
    for transcript_item in transcripts:
        if not isinstance(transcript_item, dict):
            continue
        sentences = transcript_item.get("sentences")
        if isinstance(sentences, list):
            for sentence in sentences:
                if not isinstance(sentence, dict) or not str(sentence.get("text") or "").strip():
                    continue
                start = sentence.get("begin_time")
                end = sentence.get("end_time")
                start_ms = int(start) if isinstance(start, (int, float)) else None
                end_ms = int(end) if isinstance(end, (int, float)) else None
                normalized.append(
                    {
                        "raw_start": start,
                        "raw_end": end,
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "timestamp_precision_ms": 1
                        if start_ms is not None and end_ms is not None
                        else None,
                        "text": re.sub(r"\s+", " ", str(sentence.get("text"))).strip(),
                    }
                )
        elif str(transcript_item.get("text") or "").strip():
            normalized.append(
                {
                    "raw_start": None,
                    "raw_end": None,
                    "start_ms": None,
                    "end_ms": None,
                    "timestamp_precision_ms": None,
                    "text": re.sub(r"\s+", " ", str(transcript_item.get("text"))).strip(),
                }
            )
    return normalized


def normalize_aihub(
    task_response: dict,
    *,
    model: str,
    language: str | None,
    transcription_response: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    results = task_response.get("results")
    if not isinstance(results, list) or not results:
        raise TranscriptError("completed AIHub task has no transcript results")
    normalized = _paraformer_sentences(transcription_response)
    for result in results:
        if not isinstance(result, dict):
            continue
        words = result.get("words")
        cues = _word_cues(words) if isinstance(words, list) else []
        if cues:
            normalized.extend(cues)
            continue
        segments = result.get("segments")
        if isinstance(segments, list):
            for item in segments:
                if not isinstance(item, dict) or not str(item.get("text") or "").strip():
                    continue
                start_ms = _seconds_to_ms(item.get("start"))
                end_ms = _seconds_to_ms(item.get("end"))
                normalized.append(
                    {
                        "raw_start": item.get("start"),
                        "raw_end": item.get("end"),
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                        "timestamp_precision_ms": max(
                            _number_precision_ms(item.get("start")) or 1,
                            _number_precision_ms(item.get("end")) or 1,
                        ) if start_ms is not None and end_ms is not None else None,
                        "text": re.sub(r"\s+", " ", str(item.get("text"))).strip(),
                    }
                )
            if normalized:
                continue
        text = str(result.get("text") or "").strip()
        if text:
            normalized.append(
                {
                    "raw_start": None,
                    "raw_end": None,
                    "start_ms": None,
                    "end_ms": None,
                    "timestamp_precision_ms": None,
                    "text": re.sub(r"\s+", " ", text),
                }
            )
    if not normalized:
        raise TranscriptError(
            "AIHub returned an undocumented transcript result shape; raw response was preserved"
        )
    for index, item in enumerate(normalized, 1):
        item["segment_id"] = "segment-%06d" % index
    _limit_segment_times(normalized, duration_ms)
    detected_language = next(
        (
            item.get("language_code") or item.get("language")
            for item in results
            if isinstance(item, dict)
            and (item.get("language_code") or item.get("language"))
        ),
        language,
    )
    return {
        "schema_version": 1,
        "source": {"kind": "aihub", "model": model},
        "language": detected_language,
        "upstream": {
            "raw": _redact_urls(
                {
                    "task": task_response,
                    "transcription": transcription_response,
                }
            )
        },
        "segments": normalized,
    }


def write_transcript(task_dir: Path, value: dict) -> None:
    state.atomic_write_json(task_dir / "transcript.json", value)
