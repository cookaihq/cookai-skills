#!/usr/bin/env python3
"""audio-transcribe: foxapi STT (paraformer/scribe/cohere) → normalized files.
See references/api-guide.md and the design spec. Runtime: stdlib only."""
from __future__ import annotations
import argparse, json, os, sys, time
from datetime import datetime

import config, client, validate, adapters, formats

BASE_URL = os.environ.get("FOXAPI_BASE_URL", "https://api.foxapi.cc")
CREATE_PATH = "/v1/audios/generations"
TASKS_PATH = "/v1/tasks/"
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".config", "audio-transcribe")


def _url_stem(url: str) -> str:
    tail = (url or "").split("?")[0].rstrip("/").split("/")[-1]
    stem = tail.rsplit(".", 1)[0] if "." in tail else tail
    return formats.sanitize_label(stem, 20)


def task_dir_name(ts: str, label: str, model: str) -> str:
    lab = formats.sanitize_label(label, 10) if label else formats.sanitize_label(model, 10)
    return "%s-%s" % (ts, lab) if lab else ts


def subfolder_name(i: int, audio_label, url: str) -> str:
    if audio_label:
        return formats.sanitize_label(audio_label, 20)
    stem = _url_stem(url)
    return stem if stem else "audio-%02d" % (i + 1)


def write_outputs(out_dir: str, model: str, norm_results, raw_task: dict, formats_opt: set) -> list:
    os.makedirs(out_dir, exist_ok=True)
    written = []
    def w(name, text):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            f.write(text)
        written.append(name)
    w("result.json", json.dumps(raw_task, ensure_ascii=False, indent=2) + "\n")
    w("transcript.txt", formats.to_txt(norm_results))
    if "md" in formats_opt:
        w("transcript.md", formats.to_md(model, norm_results))
    if "srt" in formats_opt:
        srt = formats.to_srt(norm_results)
        if srt is None:
            print("[save] %s 未返回逐词时间戳，已跳过 .srt" % model, file=sys.stderr)
        else:
            w("transcript.srt", srt)
    if "vtt" in formats_opt:
        vtt = formats.to_vtt(norm_results)
        if vtt is None:
            print("[save] %s 未返回逐词时间戳，已跳过 .vtt" % model, file=sys.stderr)
        else:
            w("transcript.vtt", vtt)
    return written


def _bool_flag(parser, name, dest, help_):
    # Tri-state: both default=None so an unset flag stays None (omitted from payload).
    parser.add_argument("--" + name, dest=dest, action="store_true", default=None, help=help_)
    parser.add_argument("--no-" + name, dest=dest, action="store_false", default=None, help=argparse.SUPPRESS)


def build_arg_parser():
    p = argparse.ArgumentParser(description="foxapi speech-to-text → normalized files")
    p.add_argument("--model", required=True)
    p.add_argument("--audio-url", dest="audio_urls", action="append", default=[])
    p.add_argument("--audio-label", dest="audio_labels", action="append", default=[])
    p.add_argument("--language-hint", dest="language_hints", action="append", default=[])
    p.add_argument("--language-code", dest="language_code")
    p.add_argument("--language", dest="language")
    _bool_flag(p, "diarize", "diarize", "speaker separation")
    p.add_argument("--speaker-count", dest="speaker_count", type=int)
    _bool_flag(p, "disfluency-removal", "disfluency_removal", "filter fillers")
    _bool_flag(p, "timestamp-alignment", "timestamp_alignment", "align timestamps")
    p.add_argument("--vocabulary-id", dest="vocabulary_id")
    p.add_argument("--special-word-filter", dest="special_word_filter")
    p.add_argument("--channel-id", dest="channel_id", action="append", type=int, default=[])
    _bool_flag(p, "tag-audio-events", "tag_audio_events", "tag laughter/applause")
    p.add_argument("--keyterm", dest="keyterms", action="append", default=[])
    _bool_flag(p, "punctuation", "punctuation", "punctuation + casing")
    p.add_argument("--max-new-tokens", dest="max_new_tokens", type=int)
    p.add_argument("--formats", dest="formats", default="srt,vtt,md")
    p.add_argument("--output-dir", dest="output_dir")
    p.add_argument("--label", dest="label")
    p.add_argument("--no-save", dest="no_save", action="store_true")
    p.add_argument("--poll-interval", dest="poll_interval", type=int, default=10)
    p.add_argument("--max-attempts", dest="max_attempts", type=int, default=180)
    p.add_argument("--base-url", dest="base_url", default=BASE_URL)
    p.add_argument("--use-local-key", dest="use_local_key", action="store_true")
    return p


def ns_to_params(a) -> dict:
    return {
        "model": a.model, "audio_urls": a.audio_urls, "audio_labels": a.audio_labels,
        "language_hints": a.language_hints or None, "language_code": a.language_code,
        "language": a.language, "diarize": a.diarize, "speaker_count": a.speaker_count,
        "disfluency_removal": a.disfluency_removal, "timestamp_alignment": a.timestamp_alignment,
        "vocabulary_id": a.vocabulary_id, "special_word_filter": a.special_word_filter,
        "channel_id": a.channel_id or None, "tag_audio_events": a.tag_audio_events,
        "keyterms": a.keyterms or None, "punctuation": a.punctuation, "max_new_tokens": a.max_new_tokens,
    }


def main(argv=None) -> int:
    a = build_arg_parser().parse_args(argv)
    try:
        body = validate.build_payload(ns_to_params(a))
    except validate.ValidationError as e:
        print("Error: %s" % e, file=sys.stderr); return 2

    keys = config.resolve_api_keys(os.environ, os.getcwd(), a.use_local_key, CONFIG_DIR)
    if not keys:
        print("Error: no X_API_KEY found (env / $PWD/.env.local / $PWD/.env / --use-local-key)", file=sys.stderr)
        return 2

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    payload = json.dumps(body, ensure_ascii=False).encode()
    def attempt(k):
        return client.http_request("POST", a.base_url + CREATE_PATH, {
            "Authorization": "Bearer " + k, "Content-Type": "application/json"}, payload)
    try:
        resp, used_key = client.call_with_key_fallback(keys, attempt)
    except Exception as e:
        print("Error: network failure creating task: %s" % e, file=sys.stderr); return 1
    if resp.status == 401:
        print("Error: all keys returned 401 (authentication_error).", file=sys.stderr); return 1
    if not (200 <= resp.status < 300):
        print("Error: create returned HTTP %s: %s" % (resp.status, resp.text), file=sys.stderr); return 1
    task_id = (resp.json or {}).get("id")
    if not task_id:
        print("Error: no task id in create response: %s" % resp.text, file=sys.stderr); return 1
    print("Task created: %s (model=%s, key=%s)" % (task_id, a.model, config.mask_key(used_key)))

    task = None
    for n in range(1, a.max_attempts + 1):
        q = client.http_request("GET", "%s%s%s?sync_upstream=true" % (a.base_url, TASKS_PATH, task_id),
                                 {"Authorization": "Bearer " + used_key})
        task = q.json or {}
        status = task.get("status")
        print("[poll %d/%d] status=%s" % (n, a.max_attempts, status))
        if status == "completed" and (task.get("results") or []):
            break
        if status == "failed":
            print("Task failed: %s" % json.dumps(task.get("error"), ensure_ascii=False), file=sys.stderr)
            return 1
        if n < a.max_attempts:
            time.sleep(a.poll_interval)
    else:
        print("Polling timed out. Re-query: curl '%s%s%s?sync_upstream=true' -H 'Authorization: Bearer <KEY>'"
              % (a.base_url, TASKS_PATH, task_id), file=sys.stderr)
        return 3

    results = task.get("results") or []
    if a.no_save:
        print(json.dumps(task, ensure_ascii=False, indent=2)); return 0

    fmt = set(x.strip() for x in a.formats.split(",") if x.strip())
    root = a.output_dir or os.environ.get("AUDIO_TRANSCRIBE_OUTPUT_DIR") or os.getcwd()
    label = a.label or (a.audio_labels[0] if a.audio_labels else "")
    task_dir = os.path.join(root, task_dir_name(ts, label, a.model))

    is_batch = a.model in validate.PARAFORMER and len(a.audio_urls) > 1
    saved_root = []
    if is_batch and len(results) == len(a.audio_urls):
        for i, (url, res) in enumerate(zip(a.audio_urls, results)):
            sub = subfolder_name(i, a.audio_labels[i] if i < len(a.audio_labels) else None, url)
            d = os.path.join(task_dir, sub)
            write_outputs(d, a.model, [adapters.adapt(a.model, res)], task, fmt)
            saved_root.append(d)
    else:
        if is_batch:
            print("[save] result count (%d) != url count (%d); writing flat." % (len(results), len(a.audio_urls)),
                  file=sys.stderr)
        norm = [adapters.adapt(a.model, r) for r in results]
        write_outputs(task_dir, a.model, norm, task, fmt)
        saved_root.append(task_dir)
    print("Saved to:")
    for d in saved_root:
        print("  " + d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
