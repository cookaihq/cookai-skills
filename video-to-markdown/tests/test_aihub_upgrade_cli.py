"""Opt-in complete-Plugin upgrade acceptance; all transport is synthetic/offline.

VTM_TEST_AIHUB_BASE_PLUGIN must be an intact 6.5.3 Plugin, and
VTM_TEST_AIHUB_PLUGIN an intact reviewed 6.6.0 Plugin. Neither is edited.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from video_to_markdown import state
from test_providers import isolated_configuration
from test_workflow import video

SKILL = Path(__file__).resolve().parents[1]
BASE_PLUGIN = os.environ.get("VTM_TEST_AIHUB_BASE_PLUGIN")
PLUGIN = os.environ.get("VTM_TEST_AIHUB_PLUGIN")
SCENARIOS = ("review", "query-recovery", "interrupted", "upload-unknown", "confirmation", "credential-recovery")


def check_public_upgrade(tmp_path, video, scenario, baseline, plugin):
    """Also called by native sandbox acceptance without pytest's private tmp factory."""
    baseline, plugin = Path(baseline).resolve(), Path(plugin).resolve()
    assert state.read_json(baseline / "package.json")["version"] == "6.5.3"
    assert state.read_json(plugin / "package.json")["version"] == "6.6.0"

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    packages = {root: digest(root / "package.json") for root in (baseline, plugin)}
    log = tmp_path / "requests.jsonl"
    secrets = ("synthetic-original-asr-key", "synthetic-new-review-key")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AIHUB_", "VIDEO_TO_MARKDOWN_", "VTM_FIXTURE_"))}
    env.update(AIHUB_API_KEY=secrets[0], AIHUB_RESULT_CHECK_PROVIDER="host", AIHUB_AUDIO_MODELS="paraformer-v2",
               AIHUB_MODEL_FALLBACK_POLICY="off", AIHUB_RESULT_CHECK_ENABLED="1", AIHUB_RESULT_CHECK_MODELS="gemini-4-hydrogen",
               VTM_FIXTURE_LOG=str(log), VTM_FIXTURE_SCENARIO=scenario, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --import=" + (SKILL / "tests/fixtures/aihub-offline-fetch.mjs").as_uri()).strip()
    if scenario == "confirmation":
        env.update(AIHUB_AUDIO_MODELS="paraformer-8k-v2,paraformer-v2", AIHUB_MODEL_FALLBACK_POLICY="confirm")
    if scenario == "query-recovery":
        env["VTM_FIXTURE_QUERY_FAILED"] = "1"
    if scenario == "interrupted":
        env["VTM_FIXTURE_INTERRUPT_QUERY"] = "1"

    def execute(argv, label, code=0):
        result = subprocess.run(list(map(str, argv)), cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert not any(secret in result.stdout + result.stderr for secret in secrets)
        (tmp_path / (label + ".json")).write_text(json.dumps({"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
        assert result.returncode == code, result.stdout + result.stderr
        return json.loads(result.stdout)

    def public(label, *args):
        return execute([sys.executable, SKILL / "scripts/workflow.py", *args], label)

    def dependency(root, label, *args, code=0):
        prefix = (["powershell.exe", "-NoProfile", "-NonInteractive", "-File", root / "scripts/aihub.ps1"]
                  if os.name == "nt" else ["node", root / "scripts/aihub.mjs"])
        return execute([*prefix, *args, "--skill", "aihub-audio", "--no-global-config"], label, code)

    def counts():
        requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
        return {"uploads": sum(row["path"] == "/e/v1/files/upload/base64" for row in requests),
                "asr_posts": sum(row["method"] == "POST" and row["path"] == "/e/v1/audios/generations" for row in requests),
                "review_posts": sum(row["method"] == "POST" and row["path"] == "/e/v1/llm/generations" for row in requests)}

    first = public("start-653", "start", "--video", video, "--project-root", tmp_path, "--host", "codex", "--only-provider",
                   "--no-global-config", "--skip-visuals", "Synthetic upgrade fixture", "--aihub-plugin-dir", baseline,
                   "--request", "Transcribe the complete synthetic audio faithfully")
    expected = {"review": "review_aihub_audio", "query-recovery": "continue_aihub_audio", "interrupted": "submission_unknown",
                "upload-unknown": "submission_unknown", "confirmation": "confirm_aihub_audio", "credential-recovery": "review_aihub_audio"}[scenario]
    assert first["next_action"] == expected, first
    task = Path(first["task_dir"])
    before = state.read_json(task / "manifest.json")
    run, = (task / ".state/aihub-audio").glob("*/run.json")
    run_before = state.read_json(run)
    first_receipt = task / ".state/receipt-aihub-run.json"
    frozen_receipt = digest(first_receipt)
    source_hash = digest(task / "source/audio-for-asr.mp3")
    original_requests = log.read_text(encoding="utf-8")
    initial_counts = counts()
    env["VTM_FIXTURE_INTERRUPT_QUERY"] = "0"
    env["VTM_FIXTURE_QUERY_FAILED"] = "0"

    resumed = public("resume-660", "resume", "--task-dir", task, "--aihub-plugin-dir", plugin)
    upgraded_action = ("review_aihub_audio" if scenario in {"query-recovery", "interrupted"} else expected)
    assert resumed["next_action"] == upgraded_action, resumed
    if scenario in {"review", "upload-unknown", "confirmation", "credential-recovery"}:
        assert log.read_text(encoding="utf-8") == original_requests
    assert counts() == initial_counts
    assert public("retry-660", "resume", "--task-dir", task)["next_action"] == upgraded_action
    downgrade = public("reject-downgrade", "resume", "--task-dir", task, "--aihub-plugin-dir", baseline)
    assert downgrade["next_action"] == "restore_aihub_dependency", downgrade
    assert counts() == initial_counts

    recovery_checks = None
    if scenario == "credential-recovery":
        env.update(AIHUB_RESULT_CHECK_PROVIDER="auto", VTM_FIXTURE_REVIEW_MODE="rejected")
        rejected = dependency(baseline, "old-ended-review", "review", "--record", run, "--provider", "aihub", "--wait-seconds", "0")
        assert rejected["status"] == "unavailable", rejected
        original_review = state.read_json(Path(rejected["review_record"]))
        audio = state.read_json(Path(run_before["attempts"][-1]["audio_record"]))
        frozen = {Path(path): digest(Path(path)) for path in [str(run), run_before["attempts"][-1]["audio_record"],
                  *[row["path"] for row in audio["sources"]], *[row["path"] for row in audio["files"]]]}
        env["AIHUB_API_KEY"] = secrets[1]
        unchanged_log = log.read_text(encoding="utf-8")
        denied = dependency(plugin, "deny-new-credential", "continue", "--record", run, code=2)
        assert denied["status"] == "recovery_failed", denied
        recovered = dependency(plugin, "recover-review", "review-recover", "--record", run,
                               "--reason", "Synthetic explicit authorization for a new local review credential")
        assert recovered["status"] == "review_recovery_ready", recovered
        assert log.read_text(encoding="utf-8") == unchanged_log
        assert public("preserve-unavailable", "resume", "--task-dir", task)["result_check_status"] == "unavailable"
        assert dependency(plugin, "read-old-review", "review", "--record", run)["status"] == "unavailable"
        assert log.read_text(encoding="utf-8") == unchanged_log
        env["VTM_FIXTURE_REVIEW_MODE"] = "complete"
        checked = dependency(plugin, "explicit-recheck", "review", "--record", run, "--provider", "aihub", "--recheck", "--wait-seconds", "0")
        assert checked["status"] == "mismatched", checked
        checked_requests = log.read_text(encoding="utf-8")
        mismatch = public("preserve-mismatch", "resume", "--task-dir", task)
        assert mismatch["next_action"] == "review_aihub_audio" and mismatch["result_check_status"] == "mismatched", mismatch
        assert dependency(plugin, "read-mismatch", "review", "--record", run)["status"] == "mismatched"
        assert log.read_text(encoding="utf-8") == checked_requests
        assert not (task / "transcript.json").exists()
        history, = run.parent.glob("review-history-*.json")
        assert state.read_json(history) == original_review
        assert all(digest(path) == value for path, value in frozen.items())
        remote = dependency(plugin, "deny-original-remote", "resume", "--record", audio["parts"][0]["task_record"], "--wait-seconds", "0", code=2)
        assert remote["status"] == "recovery_failed" and log.read_text(encoding="utf-8") == checked_requests
        recovery_checks = {"offline_authorization": True, "mismatch_preserved": True, "original_hashes_preserved": True,
                           "history_preserved": True, "old_remote_denied": True}

    if scenario in {"review", "query-recovery", "interrupted"}:
        env["AIHUB_RESULT_CHECK_ENABLED"] = "0"  # Explicit synthetic fixture choice; no semantic check claimed.
        finished = public("adopt-result", "resume", "--task-dir", task)
        assert finished["stage"] == "writing_document", finished
        normalized = state.read_json(task / "transcript.json")
        assert [row["start_ms"] for row in normalized["segments"]] == [200, 600]
        assert [row["text"] for row in normalized["segments"]] == ["First sentence.", "Second sentence."]

    after = state.read_json(task / "manifest.json")
    assert (after["task_id"], after["skill_version"]) == (before["task_id"], before["skill_version"])
    assert after["dependencies"]["aihub-audio"] == {"source": str(plugin), "version": "6.6.0"}
    assert state.read_json(run)["id"] == run_before["id"] and state.read_json(run)["plan_hash"] == run_before["plan_hash"]
    assert state.read_json(run)["credential_id"] == run_before["credential_id"]
    assert list((task / ".state/aihub-audio").glob("*/run.json")) == [run]
    assert digest(first_receipt) == frozen_receipt and digest(task / "source/audio-for-asr.mp3") == source_hash
    final_counts = counts()
    assert final_counts["asr_posts"] == (0 if scenario in {"upload-unknown", "confirmation"} else 1), final_counts
    assert final_counts["uploads"] == (0 if scenario == "confirmation" else 3 if scenario == "credential-recovery" else 1), final_counts
    assert final_counts["review_posts"] == (2 if scenario == "credential-recovery" else 0), final_counts
    assert all(digest(root / "package.json") == value for root, value in packages.items())
    summary = {"ok": True, "scenario": scenario, "from": "6.5.3", "to": "6.6.0", "task_id": after["task_id"],
               "creation_version": after["skill_version"], "run_id": run_before["id"], "counts": final_counts,
               "run_record": str(run), "next_action": upgraded_action, "downgrade_rejected": True,
               "review_recovery": recovery_checks, "network": "tracked offline fetch fixture"}
    (tmp_path / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


@pytest.mark.skipif(not (BASE_PLUGIN and PLUGIN), reason="opt-in complete-Plugin 6.5.3 to 6.6.0 acceptance")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_public_plugin_upgrade(tmp_path, video, scenario):
    check_public_upgrade(tmp_path, video, scenario, BASE_PLUGIN, PLUGIN)
