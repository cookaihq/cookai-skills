#!/usr/bin/env python3
"""Offline validator for the image-2 terminal handoff contract."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlsplit


BASE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = BASE_DIR / "handoff.schema.json"
MANIFEST_PATH = BASE_DIR / "golden" / "manifest.json"
SCENARIOS_PATH = BASE_DIR / "scenarios.json"

TOP_LEVEL_KEYS = {
    "schema_version",
    "task_id",
    "status",
    "outputs",
    "error",
}
OUTPUT_KEYS = {"index", "local_path", "upstream_url", "content_type", "status"}
ERROR_KEYS = {"code", "message"}
STATUSES = {"ok", "partial_success", "failed", "timed_out"}
OUTPUT_STATUSES = {"saved", "download_failed", "not_saved"}
ERROR_CODES = {
    "invalid_arguments",
    "configuration_error",
    "invalid_task_id",
    "create_transport_error",
    "create_http_error",
    "create_response_invalid",
    "query_transport_error",
    "query_http_error",
    "query_response_invalid",
    "upstream_failed",
    "poll_timeout",
    "internal_error",
}
TASK_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$", re.ASCII)
IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$", re.ASCII)
TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
QUOTED_STRING = r'"(?:[\x20-\x21\x23-\x5B\x5D-\x7E]|\\[\x20-\x7E])*"'
MEDIA_TYPE_RE = re.compile(
    rf"^{TOKEN}/{TOKEN}(?: *; *{TOKEN} *=[ ]*(?:{TOKEN}|{QUOTED_STRING}))* *$",
    re.ASCII,
)
HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
SAFE_INTEGER_MAX = 2**53 - 1

REQUIRED_COVERAGE = {
    "all_active_keys_reflection",
    "atomic_publish_failure",
    "cross_host_redirect",
    "download_http_error",
    "download_interrupted",
    "download_partial",
    "explicit_global_target",
    "explicit_project_target",
    "global_mapping_unauthorized",
    "http_downgrade",
    "json_all_saved",
    "json_no_save",
    "json_partial_download",
    "legacy_download_failure",
    "mapping_no_request",
    "multi_address_dns",
    "multi_output",
    "no_save_conflict",
    "no_target",
    "poll_timeout",
    "preterminal_error",
    "private_address",
    "project_cwd_external_output",
    "proxy_env",
    "redirect_loop",
    "redirect_overflow",
    "same_host_redirect",
    "secret_redaction",
    "task_id_invalid",
    "temporary_url",
    "upstream_failed",
    "upload_ambiguous",
    "upload_partial",
    "url_reflection",
}


class ContractError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ContractError(f"non-finite JSON number: {value}")


def load_json(path: Path) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ContractError) as exc:
        raise ContractError(f"{path.relative_to(BASE_DIR)}: {exc}") from exc


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expect_exact_keys(value: Any, expected: set[str], location: str) -> list[str]:
    if not isinstance(value, dict):
        return [f"{location}: expected object"]
    actual = set(value)
    if actual == expected:
        return []
    missing = sorted(expected - actual)
    extra = sorted(actual - expected)
    return [f"{location}: missing={missing} extra={extra}"]


def validate_schema(schema: Any) -> list[str]:
    errors = _expect_exact_keys(
        schema,
        {
            "$schema",
            "$id",
            "title",
            "type",
            "additionalProperties",
            "required",
            "properties",
            "$defs",
            "allOf",
        },
        "schema",
    )
    if errors:
        return errors

    if schema["$schema"] != "https://json-schema.org/draft/2020-12/schema":
        errors.append("schema: must use JSON Schema draft 2020-12")
    if schema["type"] != "object" or schema["additionalProperties"] is not False:
        errors.append("schema: root must be a closed object")
    if set(schema["required"]) != TOP_LEVEL_KEYS:
        errors.append("schema: root required keys do not match the v1 contract")
    if set(schema["properties"]) != TOP_LEVEL_KEYS:
        errors.append("schema: root properties do not match the v1 contract")

    definitions = schema.get("$defs")
    if not isinstance(definitions, dict) or set(definitions) != {"output", "error"}:
        errors.append("schema: $defs must contain exactly output and error")
        return errors

    output = definitions["output"]
    error = definitions["error"]
    if output.get("additionalProperties") is not False:
        errors.append("schema: output must be closed")
    if set(output.get("required", [])) != OUTPUT_KEYS:
        errors.append("schema: output required keys do not match the v1 contract")
    if set(output.get("properties", {})) != OUTPUT_KEYS:
        errors.append("schema: output properties do not match the v1 contract")
    if set(output.get("properties", {}).get("status", {}).get("enum", [])) != OUTPUT_STATUSES:
        errors.append("schema: output status enum is incomplete")
    if error.get("additionalProperties") is not False:
        errors.append("schema: error must be closed")
    if set(error.get("required", [])) != ERROR_KEYS:
        errors.append("schema: error required keys do not match the v1 contract")
    if set(error.get("properties", {})) != ERROR_KEYS:
        errors.append("schema: error properties do not match the v1 contract")
    if set(error.get("properties", {}).get("code", {}).get("enum", [])) != ERROR_CODES:
        errors.append("schema: error code enum is incomplete")
    if set(schema["properties"]["status"].get("enum", [])) != STATUSES:
        errors.append("schema: terminal status enum is incomplete")

    conditional_statuses: set[str] = set()
    for clause in schema.get("allOf", []):
        try:
            conditional_statuses.add(clause["if"]["properties"]["status"]["const"])
        except (KeyError, TypeError):
            errors.append("schema: each root condition must select one status")
    if conditional_statuses != STATUSES:
        errors.append("schema: every terminal status needs a root condition")
    return errors


def _strict_percent_decode(value: str) -> str:
    index = 0
    while index < len(value):
        if value[index] == "%":
            if index + 2 >= len(value) or any(
                character not in HEX_DIGITS for character in value[index + 1 : index + 3]
            ):
                raise ContractError("malformed percent escape")
            index += 3
        else:
            index += 1
    try:
        return unquote(value, encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ContractError("percent-decoded URL is not UTF-8") from exc


def _contains_control(value: str) -> bool:
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _validate_url(value: Any, forbidden_secrets: Iterable[str], location: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{location}: expected string"]
    errors: list[str] = []
    if not value or len(value.encode("utf-8")) > 8192:
        errors.append(f"{location}: URL must contain 1..8192 UTF-8 bytes")
    if _contains_control(value) or any(character.isspace() for character in value):
        errors.append(f"{location}: URL contains whitespace or a control character")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        del port
    except ValueError:
        errors.append(f"{location}: URL authority is invalid")
        parsed = None
    if parsed is not None:
        if parsed.scheme.lower() != "https" or not parsed.netloc or parsed.hostname is None:
            errors.append(f"{location}: URL must be absolute HTTPS")
        if parsed.username is not None or parsed.password is not None:
            errors.append(f"{location}: URL must not contain userinfo")
        if "#" in value:
            errors.append(f"{location}: URL must not contain a fragment")
    try:
        decoded = _strict_percent_decode(value)
    except ContractError as exc:
        errors.append(f"{location}: {exc}")
        decoded = ""
    for secret in forbidden_secrets:
        if secret and (secret in value or secret in decoded):
            errors.append(f"{location}: active API key reflection")
            break
    return errors


def _validate_content_type(value: Any, location: str) -> list[str]:
    if not isinstance(value, str):
        return [f"{location}: expected string"]
    if not value or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        return [f"{location}: content type must be visible ASCII"]
    if MEDIA_TYPE_RE.fullmatch(value) is None:
        return [f"{location}: invalid RFC 6838 media type"]
    return []


def _validate_error(value: Any, forbidden_secrets: Iterable[str]) -> list[str]:
    errors = _expect_exact_keys(value, ERROR_KEYS, "error")
    if errors:
        return errors
    code = value["code"]
    message = value["message"]
    if not isinstance(code, str) or code not in ERROR_CODES:
        errors.append("error.code: unknown code")
    if not isinstance(message, str):
        errors.append("error.message: expected string")
    else:
        if not message or len(message.encode("utf-8")) > 512 or _contains_control(message):
            errors.append("error.message: must be a non-empty control-free string of at most 512 bytes")
        if any(secret and secret in message for secret in forbidden_secrets):
            errors.append("error.message: active API key reflection")
    return errors


def validate_document(document: Any, forbidden_secrets: Iterable[str]) -> list[str]:
    errors = _expect_exact_keys(document, TOP_LEVEL_KEYS, "document")
    if errors:
        return errors

    if document["schema_version"] != 1 or not _is_integer(document["schema_version"]):
        errors.append("schema_version: expected integer 1")

    task_id = document["task_id"]
    if task_id is not None:
        if not isinstance(task_id, str) or TASK_ID_RE.fullmatch(task_id) is None:
            errors.append("task_id: invalid identifier")
        elif any(secret and secret in task_id for secret in forbidden_secrets):
            errors.append("task_id: active API key reflection")

    status = document["status"]
    if not isinstance(status, str) or status not in STATUSES:
        errors.append("status: unknown terminal status")

    outputs = document["outputs"]
    if not isinstance(outputs, list):
        errors.append("outputs: expected array")
        outputs = []
    else:
        for expected_index, output in enumerate(outputs, start=1):
            location = f"outputs[{expected_index - 1}]"
            output_errors = _expect_exact_keys(output, OUTPUT_KEYS, location)
            if output_errors:
                errors.extend(output_errors)
                continue
            index = output["index"]
            if (
                not _is_integer(index)
                or index < 1
                or index > SAFE_INTEGER_MAX
                or index != expected_index
            ):
                errors.append(f"{location}.index: must be contiguous, one-based, and safe")
            output_status = output["status"]
            if not isinstance(output_status, str) or output_status not in OUTPUT_STATUSES:
                errors.append(f"{location}.status: unknown status")
            local_path = output["local_path"]
            if output_status == "saved":
                if (
                    not isinstance(local_path, str)
                    or not os.path.isabs(local_path)
                    or os.path.normpath(local_path) != local_path
                ):
                    errors.append(f"{location}.local_path: saved output needs a normalized absolute path")
            elif local_path is not None:
                errors.append(f"{location}.local_path: non-saved output requires null")
            errors.extend(
                _validate_url(output["upstream_url"], forbidden_secrets, f"{location}.upstream_url")
            )
            errors.extend(_validate_content_type(output["content_type"], f"{location}.content_type"))

    error = document["error"]
    if error is not None:
        errors.extend(_validate_error(error, forbidden_secrets))

    if status == "ok":
        if error is not None or not outputs:
            errors.append("ok: requires non-empty outputs and null error")
        output_statuses = [output.get("status") for output in outputs if isinstance(output, dict)]
        if not (
            output_statuses
            and (
                all(value == "saved" for value in output_statuses)
                or all(value == "not_saved" for value in output_statuses)
            )
        ):
            errors.append("ok: outputs must be uniformly saved or uniformly not_saved")
    elif status == "partial_success":
        output_statuses = [output.get("status") for output in outputs if isinstance(output, dict)]
        if error is not None or not outputs:
            errors.append("partial_success: requires non-empty outputs and null error")
        if any(
            not isinstance(value, str) or value not in {"saved", "download_failed"}
            for value in output_statuses
        ):
            errors.append("partial_success: only saved and download_failed outputs are allowed")
        if "download_failed" not in output_statuses:
            errors.append("partial_success: at least one download_failed output is required")
    elif status == "failed":
        if outputs or error is None:
            errors.append("failed: requires empty outputs and a non-null error")
        elif isinstance(error, dict):
            if error.get("code") == "poll_timeout":
                errors.append("failed: poll_timeout belongs to timed_out")
            if error.get("code") == "upstream_failed" and task_id is None:
                errors.append("failed: upstream_failed must preserve the known task id")
            if error.get("code") == "invalid_task_id" and task_id is not None:
                errors.append("failed: invalid_task_id must emit a null task id")
    elif status == "timed_out":
        if outputs or task_id is None or not isinstance(error, dict) or error.get("code") != "poll_timeout":
            errors.append("timed_out: requires known task id, empty outputs, and poll_timeout")

    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True)
    for secret in forbidden_secrets:
        if secret and secret in serialized:
            errors.append("document: active API key sentinel leaked")
            break
    return errors


def expected_exit_code(document: dict[str, Any]) -> int:
    status = document["status"]
    if status == "ok":
        return 0
    if status == "partial_success":
        return 1
    if status == "timed_out":
        return 3
    if document["error"]["code"] == "upstream_failed":
        return 2
    return 1


def _walk_strings(value: Any, path: tuple[Any, ...] = ()) -> Iterable[tuple[tuple[Any, ...], str]]:
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_strings(item, path + (index,))
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_strings(item, path + (key,))


def validate_golden() -> tuple[list[str], int, int]:
    errors: list[str] = []
    try:
        manifest = load_json(MANIFEST_PATH)
    except ContractError as exc:
        return [str(exc)], 0, 0

    errors.extend(
        _expect_exact_keys(
            manifest,
            {
                "schema_version",
                "forbidden_secret_sentinels",
                "allowed_upstream_signature_token",
                "cases",
            },
            "golden manifest",
        )
    )
    if errors:
        return errors, 0, 0
    if manifest["schema_version"] != 1 or not _is_integer(manifest["schema_version"]):
        errors.append("golden manifest: schema_version must be integer 1")
    forbidden = manifest["forbidden_secret_sentinels"]
    if (
        not isinstance(forbidden, list)
        or not forbidden
        or any(not isinstance(value, str) or not value for value in forbidden)
        or len(set(forbidden)) != len(forbidden)
    ):
        errors.append("golden manifest: forbidden sentinels must be unique non-empty strings")
        forbidden = []
    allowed_token = manifest["allowed_upstream_signature_token"]
    if not isinstance(allowed_token, str) or not allowed_token:
        errors.append("golden manifest: allowed upstream signature token must be non-empty")

    cases = manifest["cases"]
    if not isinstance(cases, list) or not cases:
        return errors + ["golden manifest: cases must be a non-empty array"], 0, 0

    listed_paths: set[str] = set()
    valid_count = 0
    invalid_count = 0
    allowed_token_seen = False
    case_ids: set[str] = set()
    for case_index, case in enumerate(cases):
        location = f"golden manifest cases[{case_index}]"
        case_errors = _expect_exact_keys(
            case,
            {"id", "path", "expected_valid", "expected_exit_code"},
            location,
        )
        if case_errors:
            errors.extend(case_errors)
            continue
        case_id = case["id"]
        if not isinstance(case_id, str) or IDENTIFIER_RE.fullmatch(case_id) is None:
            errors.append(f"{location}.id: invalid identifier")
        else:
            if case_id in case_ids:
                errors.append(f"{location}.id: duplicate identifier")
            case_ids.add(case_id)

        relative_path = case["path"]
        if not isinstance(relative_path, str) or relative_path.startswith("/") or ".." in Path(relative_path).parts:
            errors.append(f"{location}.path: unsafe relative path")
            continue
        expected_valid = case["expected_valid"]
        if not isinstance(expected_valid, bool):
            errors.append(f"{location}.expected_valid: expected boolean")
            continue
        required_prefix = "valid/" if expected_valid else "invalid/"
        if not relative_path.startswith(required_prefix):
            errors.append(f"{location}.path: classification does not match directory")
        if relative_path in listed_paths:
            errors.append(f"{location}.path: duplicate fixture")
        listed_paths.add(relative_path)
        fixture_path = MANIFEST_PATH.parent / relative_path

        try:
            document = load_json(fixture_path)
            document_errors = validate_document(document, forbidden)
        except ContractError:
            document = None
            document_errors = ["strict JSON parse rejected"]

        if expected_valid:
            valid_count += 1
            if document_errors:
                errors.append(f"{relative_path}: expected valid; got {document_errors}")
                continue
            expected_exit = case["expected_exit_code"]
            if not _is_integer(expected_exit) or expected_exit not in {0, 1, 2, 3}:
                errors.append(f"{location}.expected_exit_code: expected 0, 1, 2, or 3")
            elif expected_exit_code(document) != expected_exit:
                errors.append(f"{relative_path}: exit-code truth table mismatch")
            for value_path, value in _walk_strings(document):
                if allowed_token in value:
                    if not value_path or value_path[-1] != "upstream_url":
                        errors.append(f"{relative_path}: upstream signature token escaped its URL field")
                    allowed_token_seen = True
        else:
            invalid_count += 1
            if case["expected_exit_code"] is not None:
                errors.append(f"{location}.expected_exit_code: invalid fixtures require null")
            if not document_errors:
                errors.append(f"{relative_path}: invalid fixture was accepted")

    fixture_paths = {
        str(path.relative_to(MANIFEST_PATH.parent))
        for classification in ("valid", "invalid")
        for path in (MANIFEST_PATH.parent / classification).glob("*.json")
    }
    if listed_paths != fixture_paths:
        errors.append(
            "golden manifest: fixture inventory mismatch "
            f"missing={sorted(fixture_paths - listed_paths)} extra={sorted(listed_paths - fixture_paths)}"
        )
    if not allowed_token_seen:
        errors.append("golden manifest: independent upstream signature token is not exercised")
    return errors, valid_count, invalid_count


def validate_scenarios() -> tuple[list[str], int]:
    errors: list[str] = []
    try:
        table = load_json(SCENARIOS_PATH)
    except ContractError as exc:
        return [str(exc)], 0
    errors.extend(_expect_exact_keys(table, {"schema_version", "scenarios"}, "scenarios"))
    if errors:
        return errors, 0
    if table["schema_version"] != 1 or not _is_integer(table["schema_version"]):
        errors.append("scenarios: schema_version must be integer 1")
    scenarios = table["scenarios"]
    if not isinstance(scenarios, list) or not scenarios:
        return errors + ["scenarios: expected non-empty array"], 0

    identifiers: set[str] = set()
    covered: set[str] = set()
    expected_keys = {
        "image_command_calls",
        "image_exit_code",
        "create_calls",
        "query_calls",
        "download_requests",
        "s3_upload_calls",
        "s3_exit_codes",
        "put_calls",
    }
    for index, scenario in enumerate(scenarios):
        location = f"scenarios[{index}]"
        scenario_errors = _expect_exact_keys(scenario, {"id", "mode", "covers", "expected"}, location)
        if scenario_errors:
            errors.extend(scenario_errors)
            continue
        identifier = scenario["id"]
        if not isinstance(identifier, str) or IDENTIFIER_RE.fullmatch(identifier) is None:
            errors.append(f"{location}.id: invalid identifier")
        else:
            if identifier in identifiers:
                errors.append(f"{location}.id: duplicate identifier")
            identifiers.add(identifier)
        if not isinstance(scenario["mode"], str) or scenario["mode"] not in {
            "json",
            "legacy",
            "agent-handoff",
        }:
            errors.append(f"{location}.mode: unknown mode")
        covers = scenario["covers"]
        if (
            not isinstance(covers, list)
            or not covers
            or any(not isinstance(value, str) or IDENTIFIER_RE.fullmatch(value) is None for value in covers)
            or len(set(covers)) != len(covers)
        ):
            errors.append(f"{location}.covers: expected unique coverage identifiers")
            covers = []
        covered.update(covers)

        expected = scenario["expected"]
        expected_errors = _expect_exact_keys(expected, expected_keys, f"{location}.expected")
        if expected_errors:
            errors.extend(expected_errors)
            continue
        for field in (
            "image_command_calls",
            "create_calls",
            "query_calls",
            "download_requests",
            "s3_upload_calls",
            "put_calls",
        ):
            value = expected[field]
            if not _is_integer(value) or value < 0:
                errors.append(f"{location}.expected.{field}: expected non-negative integer")
        if expected["image_command_calls"] not in {0, 1}:
            errors.append(f"{location}.expected.image_command_calls: expected 0 or 1")
        image_exit = expected["image_exit_code"]
        if expected["image_command_calls"] == 0:
            if image_exit is not None or any(
                expected[field] != 0 for field in ("create_calls", "query_calls", "download_requests")
            ):
                errors.append(f"{location}.expected: zero image calls require null exit and zero image work")
        elif not _is_integer(image_exit) or image_exit not in {0, 1, 2, 3}:
            errors.append(f"{location}.expected.image_exit_code: expected 0, 1, 2, or 3")
        s3_exits = expected["s3_exit_codes"]
        if (
            not isinstance(s3_exits, list)
            or any(not _is_integer(value) or value not in {0, 1, 2, 3, 4} for value in s3_exits)
            or len(s3_exits) != expected["s3_upload_calls"]
        ):
            errors.append(f"{location}.expected.s3_exit_codes: must match S3 command count")
        if expected["put_calls"] > expected["s3_upload_calls"]:
            errors.append(f"{location}.expected.put_calls: cannot exceed S3 upload calls")
        if any(tag in covers for tag in {"mapping_no_request", "temporary_url"}) and (
            expected["s3_upload_calls"] != 0 or expected["put_calls"] != 0
        ):
            errors.append(f"{location}: mapping/temporary URL scenarios must not upload")
        if "no_save_conflict" in covers and (
            expected["image_command_calls"] != 0
            or expected["s3_upload_calls"] != 0
            or expected["put_calls"] != 0
        ):
            errors.append(f"{location}: no-save conflict must stop before generation and upload")

    missing_coverage = REQUIRED_COVERAGE - covered
    if missing_coverage:
        errors.append(f"scenarios: missing required coverage {sorted(missing_coverage)}")
    return errors, len(scenarios)


def main() -> int:
    all_errors: list[str] = []
    try:
        schema = load_json(SCHEMA_PATH)
    except ContractError as exc:
        all_errors.append(str(exc))
    else:
        all_errors.extend(validate_schema(schema))

    golden_errors, valid_count, invalid_count = validate_golden()
    scenario_errors, scenario_count = validate_scenarios()
    all_errors.extend(golden_errors)
    all_errors.extend(scenario_errors)

    if all_errors:
        for error in all_errors:
            print(f"contract validation failed: {error}", file=sys.stderr)
        return 1
    print(
        "validated image-2 handoff contract: "
        f"{valid_count} valid golden, {invalid_count} invalid golden, "
        f"{scenario_count} scenarios"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
