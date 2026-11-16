"""Opt-in real installed CLI contract; transport is offline, credentials synthetic.

Set VTM_TEST_AIHUB_PLUGIN to the actual installation root and use an outer tmp
--basetemp. This never modifies the installation or submits to a real service.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from video_to_markdown import state
from test_workflow import video
from test_providers import isolated_configuration

PLUGIN = os.environ.get("VTM_TEST_AIHUB_PLUGIN")
SKILL = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not PLUGIN, reason="opt-in installed AIhub CLI contract")


@pytest.mark.parametrize("scenario", ["review", "query-recovery", "upload-unknown", "confirmation"])
def test_installed_cli_via_public_video(tmp_path, video, scenario):
    plugin = Path(PLUGIN).resolve()
    package_hash = hashlib.sha256((plugin / "package.json").read_bytes()).hexdigest()
    entry = (["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(plugin / "scripts/aihub.ps1")]
             if os.name == "nt" else ["node", str(plugin / "scripts/aihub.mjs")])
    log = tmp_path / "requests.jsonl"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AIHUB_", "VIDEO_TO_MARKDOWN_"))}
    env.update(AIHUB_API_KEY="synthetic-offline-contract-key", AIHUB_RESULT_CHECK_PROVIDER="host",
               AIHUB_AUDIO_MODELS="paraformer-v2", AIHUB_MODEL_FALLBACK_POLICY="off", AIHUB_RESULT_CHECK_ENABLED="1",
               VTM_FIXTURE_LOG=str(log), VTM_FIXTURE_SCENARIO=scenario, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    env["NODE_OPTIONS"] = (env.get("NODE_OPTIONS", "") + " --import=" + (SKILL / "tests/fixtures/aihub-offline-fetch.mjs").as_uri()).strip()
    if scenario == "confirmation":
        env.update(AIHUB_AUDIO_MODELS="paraformer-8k-v2,paraformer-v2", AIHUB_MODEL_FALLBACK_POLICY="confirm")
    if scenario == "query-recovery":
        env["VTM_FIXTURE_QUERY_FAILED"] = "1"

    def execute(argv, label):
        result = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        assert "synthetic-offline-contract-key" not in result.stdout + result.stderr
        (tmp_path / (label + ".json")).write_text(json.dumps({"exit": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
                                                            ensure_ascii=False, indent=2), encoding="utf-8")
        assert result.returncode == 0, result.stdout + result.stderr
        return json.loads(result.stdout)

    def public(label, *args):
        return execute([sys.executable, str(SKILL / "scripts/workflow.py"), *map(str, args)], label)

    first = public("start", "start", "--video", video, "--project-root", tmp_path, "--host", "codex", "--only-provider",
                   "--no-global-config", "--skip-visuals", "Synthetic installed CLI fixture",
                   "--aihub-plugin-dir", plugin, "--request", "Synthetic transcription contract fixture")
    expected = {"review": "review_aihub_audio", "query-recovery": "continue_aihub_audio",
                "upload-unknown": "submission_unknown", "confirmation": "confirm_aihub_audio"}[scenario]
    assert first["next_action"] == expected, first
    task = Path(first["task_dir"])
    manifest = state.read_json(task / "manifest.json")
    task_id = manifest["task_id"]
    run = next((task / ".state/aihub-audio").glob("*/run.json"))
    requests_before = log.read_text(encoding="utf-8")
    again = public("resume", "resume", "--task-dir", task)
    assert again["next_action"] == expected, again

    if scenario in {"confirmation", "upload-unknown", "review"}:
        assert log.read_text(encoding="utf-8") == requests_before
    if scenario in {"review", "query-recovery"}:
        # Explicit synthetic test choice; no semantic review is claimed.
        env.update(AIHUB_RESULT_CHECK_ENABLED="0", VTM_FIXTURE_QUERY_FAILED="0")
        done = public("finish", "resume", "--task-dir", task)
        assert done["stage"] == "writing_document", done
        normalized = state.read_json(task / "transcript.json")
        assert [row["text"] for row in normalized["segments"]] == ["First sentence.", "Second sentence."]
        assert [row["start_ms"] for row in normalized["segments"]] == [200, 600]
        assert all(row["speaker"].startswith("source-1/part-1/") for row in normalized["segments"])
        # Public task/resume query a known ID; neither is a second generation.
        known = execute(entry + ["task", "--skill", "aihub-audio", "--task-id", "synthetic-task", "--media", "audio",
                                "--output-dir", str(tmp_path / "known-task"), "--wait-seconds", "0", "--no-global-config"], "known-task")
        assert known["status"] == "delivered" and known["task_id"] == "synthetic-task"
        queried = execute(entry + ["resume", "--skill", "aihub-audio", "--record", known["record"],
                                  "--wait-seconds", "0", "--no-global-config"], "known-resume")
        assert queried["status"] == "delivered" and queried["task_id"] == "synthetic-task"

    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    uploads = sum(row["path"] == "/e/v1/files/upload/base64" for row in requests)
    generations = sum(row["method"] == "POST" and row["path"].endswith("/generations") for row in requests)
    assert uploads == (0 if scenario == "confirmation" else 1), requests
    assert generations == (0 if scenario in {"confirmation", "upload-unknown"} else 1), requests
    after = state.read_json(task / "manifest.json")
    assert after["task_id"] == task_id and after["skill_version"] == manifest["skill_version"]
    assert hashlib.sha256((plugin / "package.json").read_bytes()).hexdigest() == package_hash
    (tmp_path / "summary.json").write_text(json.dumps({"scenario": scenario, "ok": True, "task_id": task_id,
        "run_record": str(run), "uploads": uploads, "generation_posts": generations, "network": "offline fetch fixture"},
        ensure_ascii=False, indent=2), encoding="utf-8")
