"""Lark CLI 1.0.98 contract; credentials and login remain owned by Lark."""
from __future__ import annotations

import json
import hashlib
import re
import math
from typing import Callable
import time
from pathlib import Path

from . import config, external, state, transcript


def error(result: external.Result) -> dict:
    for stream in (result.stderr, result.stdout):
        value = external.json_output(stream)
        if isinstance(value, dict) and isinstance(value.get("error"), dict):
            return value["error"]
    return {}


def read(argv: list[str], *, cwd: Path, deadline: float, journal: dict | None = None, persist: Callable[[], None] | None = None) -> external.Result:
    journal = {} if journal is None else journal
    key = hashlib.sha256(state.canonical_json(argv)).hexdigest()
    cooldowns = journal.setdefault("cooldowns", {})
    result = external.Result(-1, timed_out=True)
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        delay = max(0, cooldowns.get(key, 0) - time.time())
        if delay >= remaining:
            break
        if delay:
            time.sleep(delay)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        result = external.execute(argv, cwd=cwd, timeout=min(30, remaining))
        failure = error(result)
        transient = result.timed_out or (failure.get("retryable") is True and (failure.get("type") == "network" or (failure.get("type") == "api" and failure.get("subtype") == "rate_limit")))
        if not transient:
            cooldowns.pop(key, None)
            if persist:
                persist()
            return result
        try:
            raw_delay = float(failure.get("retry_after_seconds", math.nan))
            delay = min(raw_delay, 60) if math.isfinite(raw_delay) and raw_delay >= 0 else 2 ** attempt
        except (TypeError, ValueError):
            delay = 2 ** attempt
        cooldowns[key] = time.time() + delay
        journal.setdefault("retry_history", []).append({"operation": " ".join(argv[3:5]), "attempt": attempt + 1,
            "max_attempts": 3, "delay_seconds": delay, "reason": failure.get("subtype", "timeout")})
        if persist:
            persist()
    return result


def reconcile(task: Path, private: dict) -> dict | None:
    route = private["transcription"]
    provider = route["providers"].setdefault("feishu-minutes", {})
    # Reconcile the durable journal before a missing CLI/login can advertise a
    # provider switch. A process may have stopped before saving its projection.
    for operation, field in (("feishu-file", "file_token"), ("feishu-minute", "minute_token")):
        key = operation + "-" + str(provider.get("retries", {}).get(operation, 0))
        if key not in private.get("operations", {}) or provider.get(field):
            continue
        receipt_path = task / ".state" / ("receipt-" + key + ".json")
        receipt = external.Result(**state.read_json(receipt_path)) if receipt_path.is_file() else external.Result(-1, timed_out=True)
        raw = external.json_output(receipt.stdout)
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        if isinstance(raw, dict) and raw.get("ok") is True and isinstance(data.get(field), str) and data[field]:
            provider[field] = data[field]
            state.save_private(task, private)
            continue
        failure = error(receipt)
        rejected = failure.get("type") in {"authentication", "authorization", "config", "validation", "policy", "confirmation"} or (
            failure.get("type") == "api" and failure.get("subtype") == "quota_exceeded" and failure.get("code") == 2091008)
        if not rejected:
            provider["uncertain_write"] = True
            state.save_private(task, private)
            return {"action": "submission_unknown", "reason": "feishu_write_outcome_unknown"}
    return None


def advance(task: Path, manifest: dict, private: dict, *, resolved, audio_path: Path, **options) -> dict | None:
    route = private["transcription"]
    provider = route["providers"].setdefault("feishu-minutes", {})
    if options.get("retry_provider") and provider.get("rejected_write"):
        rejected_operation = provider.pop("rejected_write")
        provider.setdefault("retries", {})[rejected_operation] = provider.get("retries", {}).get(rejected_operation, 0) + 1
        state.save_private(task, private)
    identity = provider.get("identity") or resolved.feishu_identity
    selected = options.get("feishu_profile") or provider.get("selected_profile")
    if selected:
        if provider.get("identity") and selected != provider["identity"]["profile"] and any(k.startswith("feishu-") for k in private.get("operations", {})):
            return {"action": "restore_feishu_identity", "reason": "submitted_task_identity_is_fixed"}
        if options.get("feishu_profile") and not any(key.startswith("feishu-") for key in private.get("operations", {})):
            provider.pop("identity", None)
            provider.pop("identity_token", None)
        provider["selected_profile"] = selected
        state.save_private(task, private)
        if not provider.get("identity"):
            identity = {"profile": selected}
    if not identity or not identity.get("profile"):
        return {"action": "select_feishu_profile"}
    prefix = ["lark-cli", "--profile", identity["profile"]]
    cwd = Path(manifest["settings"]["invocation_cwd"])
    deadline = time.monotonic() + 60
    def query(argv: list[str]) -> external.Result:
        return read(argv, cwd=cwd, deadline=deadline, journal=provider, persist=lambda: state.save_private(task, private))
    version_result = external.execute(prefix + ["--version"], cwd=cwd, timeout=10)
    if version_result.missing:
        return {"action": "install_lark_cli"}
    version = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", version_result.stdout + version_result.stderr)
    if version_result.returncode or not version or not (1, 0, 98) <= tuple(map(int, version.groups())) < (2, 0, 0):
        return {"action": "update_lark_cli", "required": ">=1.0.98,<2; verified contract 1.0.98"}
    manifest.setdefault("dependencies", {})["lark-cli"] = {"version": version.group(0), "executable": "lark-cli"}
    profiles_result = query(prefix + ["profile", "list"])
    if profiles_result.missing:
        return {"action": "install_lark_cli"}
    profiles = external.json_output(profiles_result.stdout)
    candidates = [{key: p.get(key) for key in ("name", "appId", "brand", "user", "tokenStatus")} for p in profiles if isinstance(p, dict)] if isinstance(profiles, list) else []
    matches = [p for p in profiles if isinstance(p, dict) and p.get("name") == identity["profile"]] if isinstance(profiles, list) else []
    if len(matches) != 1:
        return {"action": "repair_feishu_profile", "profile": identity["profile"], "candidates": candidates}
    auth_result = query(prefix + ["auth", "status", "--json"])
    auth = external.json_output(auth_result.stdout)
    if not isinstance(auth, dict):
        auth = {}
    user = (auth.get("identities") or {}).get("user") or {}
    if not all(identity.get(key) for key in ("app_id", "user_open_id")):
        if auth_result.returncode or not user.get("available") or not user.get("openId") or user.get("tokenStatus") not in {"valid", "needs_refresh"}:
            return {"action": "login_feishu", "profile": identity["profile"]}
        if not matches[0].get("appId") or auth.get("appId") != matches[0]["appId"] or auth.get("brand") != "feishu" or matches[0].get("brand") != "feishu":
            return {"action": "repair_feishu_identity"}
        captured = {"profile": identity["profile"], "app_id": auth["appId"], "user_open_id": user["openId"]}
        source = resolved.sources.get("feishu_identity")
        target = source or str(resolved.home_file)
        token = hashlib.sha256(state.canonical_json({"task_id": private["task_id"], "identity": captured,
            "save_target": target, "source": source, "use_home": resolved.use_home})).hexdigest()
        if options.get("confirm_feishu_identity") != token or provider.get("identity_token") != token:
            provider["identity_token"] = token
            state.save_private(task, private)
            return {"action": "confirm_feishu_identity", "identity": captured, "identity_token": token,
                    "user_name": user.get("userName"), "save_target": target, "global_reading_enabled": resolved.use_home}
        identity = captured
        if options.get("save_feishu_identity"):
            saved = {"effective": False, "path": target}
            if source != "process environment" and (source or resolved.use_home):
                try:
                    config.update_file(Path(target), {key: captured[key.rsplit("FEISHU_", 1)[1].lower()] for key in config.IDENTITY_KEYS})
                    check = config.resolve(cwd=cwd, use_home=resolved.use_home, home_config_dir=resolved.home_file.parent)
                    saved["effective"] = check.feishu_identity == captured
                except (OSError, config.ConfigError):
                    pass
            provider["identity_save"] = saved
    provider["identity"] = identity
    state.save_private(task, private)
    if (matches[0].get("appId") != identity["app_id"] or auth.get("appId") != identity["app_id"]
            or (user.get("openId") and user.get("openId") != identity["user_open_id"])
            or matches[0].get("brand") != "feishu" or auth.get("brand") != "feishu"):
        return {"action": "repair_feishu_identity", "profile": identity["profile"], "expected_identity": identity,
                "actual_identity": {"app_id": auth.get("appId"), "profile_app_id": matches[0].get("appId"), "user_open_id": user.get("openId"), "brand": auth.get("brand")}, "candidates": candidates}
    if auth_result.returncode or not user.get("available") or user.get("tokenStatus") not in {"valid", "needs_refresh"} or user.get("openId") != identity["user_open_id"]:
        return {"action": "login_feishu", "profile": identity["profile"]}
    if manifest["source"]["duration_ms"] > 6 * 3600 * 1000 or audio_path.stat().st_size > 6 * 1024 ** 3:
        return {"action": "audio_limit", "reason": "feishu_6_hours_or_6_gb"}

    for key, id_key, command in (
        ("feishu-file", "file_token", ["drive", "+upload", "--file", str(audio_path)]),
        ("feishu-minute", "minute_token", ["minutes", "+upload", "--file-token", str(provider.get("file_token", ""))]),
    ):
        if provider.get(id_key):
            continue
        if key == "feishu-minute":
            command[-1] = provider["file_token"]
        operation_key = key + "-" + str(provider.get("retries", {}).get(key, 0))
        result = external.write_once(task, private, operation_key, prefix + command + ["--as", "user", "--format", "json"],
                                     cwd=cwd, timeout=60)
        value = external.json_output(result.stdout)
        data = value.get("data", {}) if isinstance(value, dict) else {}
        identifier = data.get(id_key)
        if isinstance(value, dict) and value.get("ok") is True and isinstance(identifier, str) and identifier:
            provider[id_key] = identifier
            state.save_private(task, private)
            continue
        failure = error(result)
        if failure.get("type") == "api" and failure.get("subtype") == "quota_exceeded" and failure.get("code") == 2091008:
            provider["rejected_write"] = key
            state.save_private(task, private)
            return {"action": "feishu_quota_choice", "reason": "feishu_quota_exhausted"}
        if failure.get("type") in {"authentication", "authorization", "config", "validation", "policy", "confirmation"}:
            provider["rejected_write"] = key
            state.save_private(task, private)
            return {"action": "repair_feishu_" + failure["type"], "reason": failure["type"]}
        provider["uncertain_write"] = True
        state.save_private(task, private)
        return {"action": "submission_unknown", "reason": "feishu_write_outcome_unknown"}

    token = provider["minute_token"]
    result = query(prefix + ["minutes", "+detail", "--minute-tokens", token, "--as", "user", "--format", "json"])
    value = external.json_output(result.stdout)
    items = (value.get("data") or {}).get("minutes", []) if isinstance(value, dict) else []
    item = next((item for item in items if isinstance(item, dict) and item.get("minute_token") == token), {})
    if item.get("status") == "processing":
        return {"action": "wait_feishu"}
    if result.returncode or not isinstance(value, dict) or value.get("ok") is not True or not item or item.get("error"):
        return {"action": "retry_feishu_query"}
    raw_path = task / ".state" / "feishu-transcript.srt"
    result = query(prefix + ["api", "GET", "/open-apis/minutes/v1/minutes/" + token + "/transcript",
        "--params", json.dumps({"need_timestamp": True, "file_format": "srt"}), "--output", str(raw_path), "--as", "user"])
    receipt = external.json_output(result.stdout)
    if result.returncode or not isinstance(receipt, dict) or receipt.get("saved_path") != str(raw_path) or not raw_path.is_file():
        return {"action": "retry_feishu_export"}
    try:
        value = transcript.parse_subtitle(raw_path, language=options.get("language"))
        transcript.apply_timeline(value, manifest)
    except (transcript.TranscriptError, UnicodeError):
        return {"action": "repair_transcript_timing"}
    value["source"] = {"kind": "feishu-minutes", "timing_granularity": "segment", "raw_artifact": ".state/feishu-transcript.srt"}
    value["upstream"] = {"raw_artifact": ".state/feishu-transcript.srt"}
    transcript.write_transcript(task, value)
    return None
