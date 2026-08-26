from pathlib import Path

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
    result = transcript.normalize_aihub(task, model="scribe-v2", language=None)
    segment = result["segments"][0]
    assert segment["raw_start"] == 0
    assert segment["raw_end"] == 0.9
    assert segment["start_ms"] == 0
    assert segment["end_ms"] == 900


def test_aihub_text_without_timestamps_stays_untimed():
    task = {"status": "completed", "results": [{"text": "好好好"}]}
    result = transcript.normalize_aihub(task, model="cohere-transcribe", language="zh")
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

    result = transcript.normalize_aihub(
        task,
        model="paraformer-v2",
        language="en",
        transcription_response=transcription_response,
    )

    assert result["segments"][0]["start_ms"] == 120
    assert result["segments"][0]["raw_start"] == 120
    assert result["upstream"]["raw"]["task"]["results"][0]["file_url"] == "<redacted-temporary-url>"
    assert result["upstream"]["raw"]["transcription"]["file_url"] == "<redacted-temporary-url>"


def test_paraformer_end_time_is_limited_to_video_duration_without_changing_raw_value():
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

    result = transcript.normalize_aihub(
        task,
        model="paraformer-v2",
        language="en",
        transcription_response=transcription_response,
        duration_ms=5687,
    )

    segment = result["segments"][0]
    assert segment["raw_end"] == 5700
    assert segment["end_ms"] == 5687
    assert segment["timing_adjustments"] == [
        {
            "field": "end_ms",
            "from_ms": 5700,
            "to_ms": 5687,
            "reason": "after_video_end",
        }
    ]
