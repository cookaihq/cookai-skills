from pathlib import Path
import json
import pytest

from video_to_markdown import transcript


def test_srt_preserves_raw_and_source_precision(tmp_path: Path):
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        "1\n00:00:01,20 --> 00:00:03,40\nHello world\n",
        encoding="utf-8",
    )
    result = transcript.parse_subtitle(subtitle)
    segment = result["segments"][0]
    assert segment["raw_start"] == "00:00:01,20"
    assert segment["start_ms"] == 1200
    assert segment["timestamp_precision_ms"] == 10


def test_timestamped_plain_text_without_fraction_records_second_precision(tmp_path: Path):
    source = tmp_path / "transcript.txt"
    source.write_text("[00:00:01 --> 00:00:03] line one\n", encoding="utf-8")
    result = transcript.parse_subtitle(source)
    assert result["segments"][0]["timestamp_precision_ms"] == 1000


def test_subtitle_time_is_limited_without_changing_source_timecode(tmp_path: Path):
    subtitle = tmp_path / "sample.srt"
    subtitle.write_text(
        "1\n00:00:00,000 --> 00:00:05,700\nHello world\n",
        encoding="utf-8",
    )

    result = transcript.parse_subtitle(subtitle, duration_ms=5687)

    segment = result["segments"][0]
    assert segment["raw_end"] == "00:00:05,700"
    assert segment["end_ms"] == 5687
    assert segment["timing_adjustments"][0]["reason"] == "after_video_end"


def test_aihub_words_are_grouped_without_inventing_raw_values():
    task = {
        "status": "completed",
        "results": [
            {
                "text": "Hello world.",
                "language_code": "en",
                "words": [
                    {"text": "Hello ", "start": 0, "end": 0.42},
                    {"text": "world.", "start": 0.48, "end": 0.9},
                ],
            }
        ],
    }
    result = transcript.normalize_aihub_files([task["results"]], model="scribe-v2", language=None)
    segment = result["segments"][0]
    assert segment["raw_start"] == 0
    assert segment["raw_end"] == 0.9
    assert segment["start_ms"] == 0
    assert segment["end_ms"] == 900


def test_aihub_text_without_timestamps_stays_untimed():
    task = {"status": "completed", "results": [{"text": "好好好"}]}
    result = transcript.normalize_aihub_files([task["results"]], model="cohere-transcribe", language="zh")
    assert result["segments"][0]["start_ms"] is None
    assert result["segments"][0]["timestamp_precision_ms"] is None


def test_paraformer_transcription_uses_upstream_millisecond_timestamps_and_redacts_urls():
    task = {
        "status": "completed",
        "results": [
            {
                "file_url": "https://private.example/audio.flac",
                "transcription_url": "https://private.example/transcript.json",
            }
        ],
    }
    transcription_response = {
        "file_url": "https://private.example/audio.flac",
        "transcripts": [
            {
                "text": "Hello world.",
                "sentences": [
                    {"begin_time": 120, "end_time": 980, "text": "Hello world."}
                ],
            }
        ],
    }

    result = transcript.normalize_aihub_files(
        [transcription_response],
        model="paraformer-v2",
        language="en",
    )

    assert result["segments"][0]["start_ms"] == 120
    assert result["segments"][0]["raw_start"] == 120
    assert "private.example" not in json.dumps(result)


def test_out_of_range_provider_time_blocks_instead_of_inventing_alignment():
    task = {
        "status": "completed",
        "results": [{"transcription_url": "https://private.example/transcript.json"}],
    }
    transcription_response = {
        "transcripts": [
            {
                "sentences": [
                    {"begin_time": 0, "end_time": 5700, "text": "Hello world."}
                ]
            }
        ]
    }

    result = transcript.normalize_aihub_files(
        [transcription_response],
        model="paraformer-v2",
        language="en",
    )

    with pytest.raises(transcript.TranscriptError, match="outside"):
        transcript.apply_timeline(result, {"source": {"duration_ms": 5687}, "audio_for_asr": {"time_offset_ms": 0}})
    assert result["segments"][0]["raw_end"] == 5700


def test_incomplete_word_times_cannot_silently_drop_part_of_transcript():
    raw = [{"words": [{"text": "first", "start": 0, "end": 1}, {"text": "missing time", "start": 1}]}]
    with pytest.raises(transcript.TranscriptError):
        transcript.normalize_aihub_files([raw], model="scribe-v2", language=None)


def test_later_untimed_result_is_retained_and_blocks_timing_completion():
    raw = [{"segments": [{"start": 0, "end": 1, "text": "first"}]}, {"segments": [], "text": "must not disappear"}]
    doc = transcript.normalize_aihub_files([raw], model="test", language=None)
    assert [s["text"] for s in doc["segments"]] == ["first", "must not disappear"]
    with pytest.raises(transcript.TranscriptError):
        transcript.apply_timeline(doc, {"source": {"duration_ms": 3000}, "audio_for_asr": {"time_offset_ms": 0}})


def test_word_granularity_validates_each_word_not_only_sentence_boundaries():
    raw = [{"words": [{"text": "first ", "start": 0, "end": 2}, {"text": "reversed ", "start": 1, "end": .5}, {"text": "last", "start": 1, "end": 3}]}]
    with pytest.raises(transcript.TranscriptError):
        doc = transcript.normalize_aihub_files([raw], model="test", language=None)
        transcript.apply_timeline(doc, {"source": {"duration_ms": 4000}, "audio_for_asr": {"time_offset_ms": 0}})
