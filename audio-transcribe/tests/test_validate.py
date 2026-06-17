import pytest
import validate as v


def test_paraformer_basic_payload():
    body = v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a"],
                            "language_hints": ["zh", "en"], "diarize": True, "speaker_count": 2})
    assert body["model"] == "paraformer-v2"
    assert body["file_urls"] == ["http://a"]
    assert body["language_hints"] == ["zh", "en"]
    assert body["diarization"] == {"enabled": True, "speaker_count": 2}


def test_scribe_single_url_and_keyterms():
    body = v.build_payload({"model": "scribe-v2", "audio_urls": ["http://a"],
                            "diarize": False, "keyterms": ["foo"]})
    assert body["audio_url"] == "http://a" and body["diarize"] is False and body["keyterms"] == ["foo"]


def test_cohere_language_whitelist():
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "cohere-transcribe", "audio_urls": ["http://a"], "language": "ru"})
    ok = v.build_payload({"model": "cohere-transcribe", "audio_urls": ["http://a"], "language": "vi"})
    assert ok["language"] == "vi"


def test_cross_model_flag_rejected():
    with pytest.raises(v.ValidationError):  # keyterms is scribe-only
        v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a"], "keyterms": ["x"]})
    with pytest.raises(v.ValidationError):  # language_hints not allowed on 8k
        v.build_payload({"model": "paraformer-8k-v2", "audio_urls": ["http://a"], "language_hints": ["zh"]})


def test_multi_url_only_paraformer():
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "scribe-v2", "audio_urls": ["http://a", "http://b"]})
    ok = v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a", "http://b"]})
    assert ok["file_urls"] == ["http://a", "http://b"]


def test_speaker_count_requires_diarize_and_range():
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a"], "speaker_count": 3})
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a"], "diarize": True, "speaker_count": 1})


def test_keyterm_limits_and_special_word_filter_json():
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "scribe-v2", "audio_urls": ["http://a"], "keyterms": ["x" * 51]})
    with pytest.raises(v.ValidationError):
        v.build_payload({"model": "paraformer-v2", "audio_urls": ["http://a"], "special_word_filter": "{bad"})
