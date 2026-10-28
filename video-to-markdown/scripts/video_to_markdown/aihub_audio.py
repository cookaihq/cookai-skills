"""Delegate to the installed AIhub Audio Skill's public CLI, never to its HTTP API."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import external, state, transcript

DEFINITE_FAILURES = {"not_submitted", "remote_failed", "failed", "no_compatible_model", "attempt_limit", "preflight_failed", "configuration_required"}


def _inside(path_value: str, root: Path) -> Path:
    path = Path(path_value).resolve()
    if not path.is_relative_to(root.resolve()):
        raise state.StateError("AIhub result path is outside this task's private dependency directory")
    return path


def advance(task: Path, manifest: dict, private: dict, *, audio_path: Path, **options) -> dict | None:
    route = private["transcription"]
    provider = route["providers"].setdefault("aihub-audio", {})
    has_writes = any(key.startswith("aihub-") for key in private.get("operations", {}))

    def local_failure(reason: str) -> dict:
        return ({"action": "restore_aihub_dependency", "reason": reason} if has_writes
                else {"action": "repair_aihub_dependency", "failed": reason})

    if options.get("request"):
        if has_writes and options["request"] != provider.get("original_request"):
            return {"action": "restore_original_request"}
        provider["original_request"] = options["request"]
        state.save_private(task, private)
    if options.get("aihub_unavailable") and not provider.get("run_record"):
        return local_failure("aihub_dependency_unavailable")
    root_value = options.get("aihub_plugin_dir") or provider.get("plugin_dir")
    if not root_value:
        return {"action": "load_aihub_audio"}
    root = Path(root_value).expanduser().resolve()
    try:
        package = json.loads((root / "package.json").read_text(encoding="utf-8"))
        skill = (root / "skills/aihub-audio/SKILL.md").read_text(encoding="utf-8")
        version = package.get("version", "")
        valid = re.fullmatch(r"1\.(\d+)\.(\d+)", version)
        if package.get("name") != "aihub-studio" or not valid or int(valid[1]) < 3 or not re.search(r"^name:\s*aihub-audio\s*$", skill, re.M):
            return local_failure("aihub_dependency_contract_unsupported")
    except (OSError, ValueError):
        return local_failure("aihub_dependency_unavailable")
    entry = root / "scripts" / ("aihub.ps1" if os.name == "nt" else "aihub.mjs")
    if not entry.is_file():
        return local_failure("aihub_dependency_unavailable")
    if not provider.get("original_request"):
        return {"action": "record_original_request"}
    if provider.get("plugin_dir") and provider["plugin_dir"] != str(root) and provider.get("run_record"):
        return {"action": "restore_aihub_dependency"}
    provider.update(plugin_dir=str(root), version=version)
    manifest.setdefault("dependencies", {})["aihub-audio"] = {"version": version, "source": str(root)}
    if options.get("no_global_config"):
        manifest["settings"]["no_global_config"] = True
    state.save_private(task, private)
    prefix = (["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(entry)] if os.name == "nt" else ["node", str(entry)])
    suffix = ["--no-global-config"] if manifest["settings"].get("no_global_config") else []
    cwd = Path(manifest["settings"]["invocation_cwd"])

    def command(name: str, *args: str) -> list[str]:
        return prefix + [name, "--skill", "aihub-audio", *args, *suffix]

    output_dir = task / ".state" / "aihub-audio"
    output_dir.mkdir(exist_ok=True)
    if provider.get("known_task_recovery"):
        return {"action": "recover_aihub_known_task", "task_id": provider["task_id"], "run_record": provider.get("run_record"),
                "dependency_command": command("task", "--task-id", provider["task_id"], "--media", "audio", "--output-dir", str(output_dir / "recovery"), "--wait-seconds", "30"),
                "reason": "restore_known_task_before_continuing_the_run"}
    # A run record is written by AIhub before its first generation submission.
    # Recover it after a lost wrapper response; never issue another `run`.
    if not provider.get("run_record") and "aihub-run" in private.get("operations", {}):
        candidates = list(output_dir.glob("aihub-run-*/run.json"))
        if len(candidates) == 1:
            candidate = state.read_json(candidates[0])
            if candidate.get("kind") == "aihub-run" and candidate.get("skill") == "aihub-audio":
                provider["run_record"] = str(candidates[0].resolve())
                state.save_private(task, private)
    receipt_keys = ["aihub-run"] + sorted((key for key in private.get("operations", {}) if re.fullmatch(r"aihub-continue-\d+", key)), key=lambda key: int(key.rsplit("-", 1)[1]))
    pending_receipt = next((key for key in receipt_keys if key not in provider.get("consumed_receipts", []) and (task / ".state" / ("receipt-" + key + ".json")).is_file()), None)
    if pending_receipt:
        key = pending_receipt
        result = external.Result(**state.read_json(task / ".state" / ("receipt-" + key + ".json")))
    elif provider.get("run_record"):
        record = _inside(provider["run_record"], output_dir)
        if not record.is_file():
            return {"action": "restore_aihub_run_record"}
        # The dependency owns continue's write safety, model confirmation, and
        # original-task queries. A timeout here is not a new-run fallback signal.
        key = "aihub-continue-" + str(provider.get("continuations", 0))
        result = external.write_once(task, private, key, command("continue", "--record", str(record), "--wait-seconds", "30"), cwd=cwd, timeout=60)
    else:
        if audio_path.stat().st_size > 20 * 1024 * 1024:
            return {"action": "audio_limit", "failed": "aihub_audio_exceeds_20_mib"}
        doctor = external.execute(command("doctor"), cwd=cwd, timeout=30)
        doctor_value = external.json_output(doctor.stdout)
        if doctor.returncode or not isinstance(doctor_value, dict) or doctor_value.get("status") != "ok":
            return local_failure("aihub_local_preflight_failed")
        upload_input = task / ".state" / "aihub-upload.json"
        state.atomic_write_json(upload_input, {"path": str(audio_path), "file_name": audio_path.name})
        uploaded = external.write_once(task, private, "aihub-upload", command("upload", "--input-file", str(upload_input)), cwd=cwd, timeout=60)
        upload = external.json_output(uploaded.stdout)
        if uploaded.timed_out or not isinstance(upload, dict):
            return {"action": "submission_unknown"}
        if upload.get("status") in DEFINITE_FAILURES:
            return {"action": "repair_aihub_dependency", "failed": "aihub_upload_not_submitted"}
        if upload.get("status") != "ok" or not isinstance(upload.get("url"), str) or not upload["url"].startswith(("https://", "http://")):
            return {"action": "submission_unknown"}
        request_path = task / ".state" / "aihub-request.json"
        request = {"operation": "audio-transcribe", "inputs": {"audios": [upload["url"]]},
                   "original_request": provider["original_request"],
                   "review_requirements": [{"id": "transcription", "text": "Transcribe all audio faithfully with actual timestamps; preserve original language and speakers. Do not summarize or translate.", "priority": "required"}]}
        state.atomic_write_json(request_path, request)
        planned = external.execute(command("plan", "--request-file", str(request_path)), cwd=cwd, timeout=30)
        plan = external.json_output(planned.stdout)
        if planned.returncode or not isinstance(plan, dict) or plan.get("status") != "ok":
            return {"action": "repair_aihub_dependency", "failed": "aihub_plan_failed"}
        key = "aihub-run"
        result = external.write_once(task, private, key, command("run", "--request-file", str(request_path), "--output-dir", str(output_dir), "--wait-seconds", "30"), cwd=cwd, timeout=60)

    value = external.json_output(result.stdout)
    if not isinstance(value, dict):
        value = {}
    if value.get("run_record"):
        provider["run_record"] = str(_inside(value["run_record"], output_dir))
    if value.get("task_id"):
        provider["task_id"] = value["task_id"]
    provider["last_status"] = value.get("status")
    if value.get("status") == "persistence_failed" and provider.get("task_id"):
        provider["known_task_recovery"] = True
    if key.startswith("aihub-continue-"):
        provider["continuations"] = int(key.rsplit("-", 1)[1]) + 1
    if key not in provider.setdefault("consumed_receipts", []):
        provider["consumed_receipts"].append(key)
    state.save_private(task, private)
    details = {"run_record": provider.get("run_record"), "dependency_receipt": str(task / ".state" / ("receipt-" + key + ".json"))}
    if result.timed_out or not value.get("status") or value.get("status") == "submission_unknown":
        return {"action": "submission_unknown", **details}
    status = value["status"]
    if status == "persistence_failed" and provider.get("task_id"):
        return {"action": "recover_aihub_known_task", "task_id": provider["task_id"], **details,
                "dependency_command": command("task", "--task-id", provider["task_id"], "--media", "audio", "--output-dir", str(output_dir / "recovery"), "--wait-seconds", "30")}
    if status in DEFINITE_FAILURES:
        return {"action": "repair_aihub_dependency", "failed": "aihub_" + status, **details}
    if status == "confirmation_required":
        return {"action": "confirm_aihub_audio", **details}
    if status != "delivered":
        return {"action": "continue_aihub_audio", **details}
    check = value.get("result_check") or {}
    provider["result_check_status"] = check.get("status", "unknown")
    if check.get("status") != "disabled":
        provider["user_notice"] = "如果想关闭 AIhub Audio 的结果检查，可以在对话中说‘关闭结果检查’或‘仅本次关闭结果检查’；本流程未替你关闭检查。"
    state.save_private(task, private)
    if check.get("status") not in {"matched", "disabled"}:
        return {"action": "review_aihub_audio", "result_check_status": check.get("status", "unknown"), **details}
    files = value.get("files") or (value.get("result") or {}).get("files") or []
    raw_documents = []
    raw_paths = []
    try:
        for file in files:
            path = _inside(file["path"], output_dir)
            if path.suffix.lower() != ".json" or not path.is_file() or path.stat().st_size != file["bytes"]:
                return {"action": "repair_aihub_result", **details}
            raw_documents.append(state.read_json(path))
            raw_paths.append(str(path.relative_to(task)))
        document = transcript.normalize_aihub_files(raw_documents, model=value.get("model"), language=options.get("language"))
        transcript.apply_timeline(document, manifest)
    except (state.StateError, transcript.TranscriptError, KeyError, TypeError):
        return {"action": "repair_transcript_timing", **details}
    document["source"]["raw_artifacts"] = raw_paths
    document["upstream"] = {"raw_artifacts": raw_paths}
    transcript.write_transcript(task, document)
    return None
