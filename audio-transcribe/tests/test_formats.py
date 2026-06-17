import formats as F
from adapters import Word, NormalizedResult


def nr(words, text="hi", lang="en"):
    has = any(w.start is not None for w in words)
    return NormalizedResult(text, lang, 0.9, words, [], any(w.speaker for w in words), has)


def test_sanitize_label_keeps_cjk_strips_unsafe_and_truncates():
    assert F.sanitize_label('a/b:c*d e f g h i j k') == "a_b_c_d_e_"[:10]
    assert F.sanitize_label("会议记录二零二六年纪要") == "会议记录二零二六年纪"  # 10 code points
    assert F.sanitize_label("") == ""


def test_txt_joins_results_with_blank_line():
    out = F.to_txt([nr([], "first"), nr([], "second")])
    assert out == "first\n\nsecond\n"


def test_srt_none_without_timestamps():
    assert F.to_srt([nr([], "no timing")]) is None
    assert F.to_vtt([nr([], "no timing")]) is None


def test_srt_breaks_on_gap_and_sentence_end():
    words = [Word("Hello", 0.0, 0.4, None), Word(" world.", 0.4, 0.9, None),
             Word(" Next", 3.0, 3.3, None), Word(" one", 3.3, 3.6, None)]
    srt = F.to_srt([nr(words, "Hello world. Next one")])
    assert "00:00:00,000 --> 00:00:00,900" in srt   # cue 1 ends at sentence/gap
    assert "Hello world." in srt
    assert srt.count("\n\n") >= 1                    # at least two cues
    vtt = F.to_vtt([nr(words)])
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:00.900" in vtt


def test_md_has_header_and_speaker_segmentation():
    words = [Word("Hi", 0.0, 0.5, "0"), Word(" there", 0.5, 1.0, "1")]
    md = F.to_md("scribe-v2", [nr(words, "Hi there")])
    assert md.startswith("# ") or "scribe-v2" in md
    assert "说话人 0" in md and "说话人 1" in md
