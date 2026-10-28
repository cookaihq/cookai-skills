"""Durable coordination of the two transcription dependencies."""
from pathlib import Path

from . import aihub_audio, config, feishu, state


def public_status(private: dict) -> dict:
    route = private["transcription"]
    result = {"provider": route["provider"], "paused": bool(route.get("paused"))}
    if "default_save" in route:
        result["default_save"] = route["default_save"]
    for provider in route["providers"].values():
        for key in ("identity_save", "result_check_status", "user_notice", "retry_history"):
            if key in provider:
                result[key] = provider[key]
    return result


def pending(task_dir: Path, manifest: dict, private: dict, action: str, **details) -> dict:
    route = private["transcription"]
    route["status"] = action
    state.save_private(task_dir, private)
    manifest["asr"] = {"provider": route["provider"], "state": action,
                       "reason": route.get("reason"), "failures": route.get("failures", {})}
    manifest["stage"] = "asr_pending"
    state.save_manifest(task_dir, manifest)
    return {"task_dir": str(task_dir), "stage": manifest["stage"], "provider": route["provider"],
            "next_action": action, "reason": route.get("reason"),
            **public_status(private), **details}


def advance(task_dir: Path, manifest: dict, private: dict, **options) -> dict | None:
    route = private["transcription"]
    choice = options.get("choice")
    if choice == "pause":
        route["paused"] = True
    elif choice in {"continue", "aihub-once", "aihub-default"}:
        route["paused"] = False
    if route["provider"] == "feishu-minutes":
        uncertain = feishu.reconcile(task_dir, private)
        if uncertain:
            route["feishu_choice_allowed"] = False
            return pending(task_dir, manifest, private, uncertain.pop("action"), **uncertain)
        if route["providers"].get("feishu-minutes", {}).get("minute_token"):
            route["feishu_choice_allowed"] = False
    if route.get("paused"):
        return pending(task_dir, manifest, private, "paused")
    if choice in {"aihub-once", "aihub-default"}:
        if route["provider"] != "feishu-minutes" or not route.get("feishu_choice_allowed"):
            raise state.StateError("provider switch is not available while a submission or result is unresolved")
        if "aihub-audio" in route.get("failures", {}) or manifest["settings"].get("only_provider"):
            return pending(task_dir, manifest, private, "both_providers_failed")
        if choice == "aihub-default":
            route["default_save"] = config.save_default(options["resolved"], cwd=Path(manifest["settings"]["invocation_cwd"]), provider="aihub-audio")
        route["provider"] = "aihub-audio"
        route["manual_switch_from"] = "feishu-minutes"
        route["feishu_choice_allowed"] = False
        state.save_private(task_dir, private)
    if route["provider"] == "aihub-audio":
        result = aihub_audio.advance(task_dir, manifest, private, **options)
        if result and result.get("failed"):
            reason = result.pop("failed")
            route.setdefault("failures", {})["aihub-audio"] = reason
            route["reason"] = reason
            if "feishu-minutes" in route["failures"]:
                return pending(task_dir, manifest, private, "both_providers_failed")
            if (not manifest["settings"].get("only_provider") and not route["automatic_switches"]
                    and route.get("manual_switch_from") != "feishu-minutes"):
                route["provider"] = "feishu-minutes"
                route["automatic_switches"] += 1
            else:
                return pending(task_dir, manifest, private, result.pop("action"), **result)
        elif result:
            return pending(task_dir, manifest, private, result.pop("action"), **result)
    if route["provider"] == "feishu-minutes":
        result = feishu.advance(task_dir, manifest, private, **options)
        feishu_state = route["providers"].get("feishu-minutes", {})
        route["feishu_choice_allowed"] = bool(result and result.get("action") not in {"submission_unknown", "wait_feishu", "retry_feishu_query", "retry_feishu_export", "repair_transcript_timing"}
            and not feishu_state.get("minute_token") and not feishu_state.get("uncertain_write"))
        if result and route["feishu_choice_allowed"] and not manifest["settings"].get("only_provider") and "aihub-audio" not in route.get("failures", {}):
            result["choices"] = ["aihub-once", "pause"]
        if result and result.get("action") == "feishu_quota_choice":
            route.setdefault("failures", {})["feishu-minutes"] = "feishu_quota_exhausted"
            if "aihub-audio" in route["failures"] or manifest["settings"].get("only_provider"):
                result["action"] = "both_providers_failed"
            else:
                result["choices"] = ["aihub-once", "aihub-default", "pause"]
    if result is None:
        route["status"] = "transcript_ready"
        state.save_private(task_dir, private)
        manifest["asr"] = {"provider": route["provider"], "state": "transcript_ready"}
        state.save_manifest(task_dir, manifest)
        return None
    action = result.pop("action")
    return pending(task_dir, manifest, private, action, **result)
