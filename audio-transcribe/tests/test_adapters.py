import json, os
import pytest
import adapters

FX = os.path.join(os.path.dirname(__file__), "fixtures")


def _load(name):
    d = json.load(open(os.path.join(FX, name), encoding="utf-8"))
    assert d.get("status") == "completed", d
    return d["model"], (d.get("results") or [])


def _check(model, results):
    assert results, "fixture has no results"
    nr = adapters.adapt(model, results[0])
    assert isinstance(nr.text, str)
    for w in nr.words:
        if w.start is not None and w.end is not None:
            assert w.end >= w.start >= 0
            assert w.end < 24 * 3600  # sane seconds, not raw ms for a short clip
    return nr


def test_cohere_adapter_real_fixture():
    # Phase 0 captured cohere.json (the only model foxapi currently serves).
    model, results = _load("cohere.json")
    nr = _check(model, results)
    # cohere returns text only, no word/segment timeline
    assert nr.words == [] and nr.segments == []
    assert nr.has_timestamps is False


def test_scribe_adapter_official_schema():
    # scribe-v2 is unavailable on foxapi (no_compatible_channel) so no Phase 0
    # fixture exists; this exercises the adapter against the OFFICIAL documented
    # results[] schema from spec §3 (text/language_code/language_probability/words
    # with start,end in seconds). Replace with a real fixture once scribe is provisioned.
    r = {"text": "Hello world.", "language_code": "en", "language_probability": 0.98,
         "words": [{"text": "Hello", "start": 0.0, "end": 0.42, "type": "word", "speaker_id": "0"},
                   {"text": " world.", "start": 0.42, "end": 0.90, "type": "word", "speaker_id": "0"}]}
    nr = adapters.adapt("scribe-v2", r)
    assert nr.text == "Hello world." and nr.language == "en"
    assert nr.has_timestamps is True and nr.speakers_present is True
    assert nr.words[0].start == 0.0 and nr.words[1].end == 0.90
    assert nr.words[0].speaker == "0"


def test_paraformer_adapter_blocked():
    pytest.skip("paraformer-v2 unavailable on foxapi (pricing_not_configured); "
                "Phase 0 schema not captured — see PHASE0-BLOCKER.md")
