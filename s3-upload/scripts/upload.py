#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import replace
from functools import partial
import json
import os
import sys

from artifacts import (
    ArtifactError,
    CheckpointStore,
    build_object_reference,
    parse_checkpoint,
    parse_object_reference,
    preflight_result_output,
)
from capabilities import LiveTestInterlock
from operations import (
    OperationError,
    execute_delete,
    execute_single_put,
    generate_object_url,
    reconcile_delete,
    reconcile_put,
)
from multipart import (
    MultipartError, abort_multipart, execute_multipart, reconcile_multipart,
    resume_multipart,
)
from delivery_schema import serialize_artifact
from planning import (
    LocalFileError, PlanError, build_delete_dry_run, build_upload_dry_run,
    provider_candidate_for_target,
)
from probe import build_probe
from provider_candidates import build_candidate_request
from resolver import ResolutionError, resolve_target
from results import ResultError, build_result, exit_code_for_result, validate_result
from safe_io import FileSecurityError, atomic_write, read_regular_file
from source_file import SourceError
from v2_schema import EXPERIMENTAL_PROVIDERS, SchemaError
from s3 import (
    build_signed_request, http_request,
)


def _live_test_context(environ):
    enabled = environ.get("S3_UPLOAD_LIVE_TEST") == "1"
    target_ref = environ.get("S3_UPLOAD_LIVE_TEST_TARGET") or None
    return (
        "test-only" if enabled else "normal",
        LiveTestInterlock(enabled=enabled, target_ref=target_ref),
        enabled,
    )


def _active_credentials(resolved):
    credential = resolved.credential
    if credential is None:
        return ()
    return (
        credential.access_key_id,
        credential.secret_access_key,
        credential.session_token,
    )

def v2_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Use scoped Upload Targets to upload or reference one S3-compatible object"
    )
    commands = p.add_subparsers(dest="operation", required=True)
    probe = commands.add_parser(
        "probe", help="report machine-readable readiness without any file or network side effect"
    )
    probe.add_argument("--target")
    probe.add_argument("--caller-skill")
    probe.add_argument("--use-local-key", action="store_true")
    upload = commands.add_parser(
        "upload", help="upload one local file; provider capabilities gate the selected mode"
    )
    upload.add_argument("--file", required=True)
    upload.add_argument("--target")
    upload.add_argument("--caller-skill")
    upload.add_argument("--key")
    upload.add_argument("--content-type")
    upload.add_argument("--cache-control")
    upload.add_argument("--content-disposition")
    upload.add_argument("--collision", choices=["replace", "unique", "reject"])
    upload.add_argument("--presign-expires", type=int)
    upload.add_argument("--reference-out")
    upload.add_argument("--result-out")
    upload.add_argument("--json", action="store_true")
    upload.add_argument("--dry-run", action="store_true")
    upload.add_argument("--use-local-key", action="store_true")
    upload.add_argument("--allow-insecure-http", action="store_true")
    url = commands.add_parser("url", help="generate a current-key URL without remote I/O")
    url.add_argument("--reference-file")
    url.add_argument("--target")
    url.add_argument("--key")
    url.add_argument("--presign-expires", type=int)
    url.add_argument("--json", action="store_true")
    url.add_argument("--use-local-key", action="store_true")
    url.add_argument("--allow-insecure-http", action="store_true")
    delete = commands.add_parser(
        "delete", help="capability-gated; unavailable in normal baseline contracts"
    )
    delete.add_argument("--reference-file", required=True)
    delete.add_argument("--target")
    choice = delete.add_mutually_exclusive_group(required=True)
    choice.add_argument("--confirm-delete", action="store_true")
    choice.add_argument("--dry-run", action="store_true")
    delete.add_argument("--json", action="store_true")
    delete.add_argument("--use-local-key", action="store_true")
    delete.add_argument("--allow-insecure-http", action="store_true")
    resume = commands.add_parser(
        "resume", help="capability-gated multipart recovery; unavailable in normal baseline contracts"
    )
    resume.add_argument("--checkpoint", required=True)
    resume.add_argument("--target")
    resume.add_argument("--json", action="store_true")
    resume.add_argument("--use-local-key", action="store_true")
    resume.add_argument("--allow-insecure-http", action="store_true")
    reconcile = commands.add_parser(
        "reconcile", help="capability-gated read-only recovery from a checkpoint"
    )
    reconcile.add_argument("--checkpoint", required=True)
    reconcile.add_argument("--target")
    reconcile.add_argument("--json", action="store_true")
    reconcile.add_argument("--use-local-key", action="store_true")
    reconcile.add_argument("--allow-insecure-http", action="store_true")
    abort = commands.add_parser(
        "abort", help="capability-gated multipart cleanup; unavailable in normal baseline contracts"
    )
    abort.add_argument("--checkpoint", required=True)
    abort.add_argument("--target")
    abort.add_argument("--confirm-abort", action="store_true", required=True)
    abort.add_argument("--json", action="store_true")
    abort.add_argument("--use-local-key", action="store_true")
    abort.add_argument("--allow-insecure-http", action="store_true")
    return p


def _v2_url(args, *, environ, cwd, config_home, now) -> int:
    try:
        if args.reference_file:
            if args.key is not None:
                raise ResolutionError("url --reference-file does not accept --key")
            text = read_regular_file(
                args.reference_file, max_bytes=65536, secret=True, missing_ok=False,
            )
            reference = parse_object_reference(text or "")
            selected_ref = args.target or reference["target_ref"]
            if args.target is None and selected_ref.startswith("global:") and not args.use_local_key:
                raise ResolutionError("Object Reference does not authorize reading a Global Target; pass --use-local-key")
            resolved = resolve_target(
                cwd=cwd, config_home=config_home, environ=environ,
                cli_target=selected_ref, cli_caller=None,
                use_local_key=args.use_local_key, now=now,
            )
            if args.target is None:
                resolved = replace(resolved, source="reference-original")
            reference = parse_object_reference(
                text or "", credentials=_active_credentials(resolved)
            )
        else:
            if not args.target or args.key is None:
                raise ResolutionError("url requires --reference-file or both --target and --key")
            resolved = resolve_target(
                cwd=cwd, config_home=config_home, environ=environ,
                cli_target=args.target, cli_caller=None,
                use_local_key=args.use_local_key, now=now,
            )
            key = args.key
            if (resolved.target.access.mode == "public" or resolved.target.retention.mode == "expire") and not key.startswith(resolved.target.prefix):
                raise ResolutionError("Object Key is outside the Target policy prefix")
            reference = build_object_reference(
                target_ref=resolved.ref.text,
                target=resolved.target,
                key=key,
                version_id=None,
            )
        if resolved.target.endpoint.startswith("http://") and not args.allow_insecure_http:
            raise ResolutionError("HTTP Target requires --allow-insecure-http")
        result = generate_object_url(
            resolved=resolved,
            reference=reference,
            presign_expires=args.presign_expires,
            now=now,
        )
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        else:
            print(result["url"], flush=True)
        return 0
    except (
        ArtifactError,
        FileSecurityError,
        OSError,
        OperationError,
        PlanError,
        ResolutionError,
        SchemaError,
    ) as exc:
        print(f"[s3-upload] config_error: {exc}", file=sys.stderr)
        return 2


def _v2_delete(args, *, environ, cwd, config_home, transport, now) -> int:
    try:
        execution_mode, live_interlock, allow_candidates = _live_test_context(environ)
        text = read_regular_file(
            args.reference_file, max_bytes=65536, secret=True, missing_ok=False,
        )
        reference = parse_object_reference(text or "")
        selected_ref = args.target or reference["target_ref"]
        if args.target is None and selected_ref.startswith("global:") and not args.use_local_key:
            raise ResolutionError("Object Reference does not authorize reading a Global Target; pass --use-local-key")
        resolved = resolve_target(
            cwd=cwd, config_home=config_home, environ=environ,
            cli_target=selected_ref, cli_caller=None,
            use_local_key=args.use_local_key, now=now,
            allow_candidates=allow_candidates,
        )
        if args.target is None:
            resolved = replace(resolved, source="reference-original")
        reference = parse_object_reference(
            text or "", credentials=_active_credentials(resolved)
        )
        dry_run = build_delete_dry_run(
            resolved=resolved,
            reference=reference,
            allow_insecure_http=args.allow_insecure_http,
            now=now,
            execution_mode=execution_mode,
            live_test_interlock=live_interlock,
        )
        if args.dry_run:
            result = build_result(
                "delete", "dry_run", object_reference=reference,
                retention=reference["retention"], delete_scope=dry_run.plan["delete_scope"],
                plan=dry_run.plan,
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
            else:
                print("[s3-upload] dry_run " + json.dumps(dry_run.plan, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
            return exit_code_for_result(result)
        if not dry_run.executable:
            print("[s3-upload] config_error: delete plan is blocked", file=sys.stderr)
            return 2
        if (
            execution_mode == "test-only"
            and resolved.target.provider in EXPERIMENTAL_PROVIDERS
        ):
            raise PlanError("candidate execution requires the authorized evidence harness")
        outcome = execute_delete(
            resolved=resolved,
            reference=reference,
            plan=dry_run.plan,
            transport=transport,
            project_root=cwd,
            now=now,
            checkpoint_notice=lambda value: print(
                f"[s3-upload] checkpoint_id={value}", file=sys.stderr, flush=True
            ),
        )
        result = outcome.result
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        else:
            print(
                f"[s3-upload] {result['status']} delete_scope={result['delete_scope']}",
                file=sys.stderr,
                flush=True,
            )
        outcome.finalize()
        return exit_code_for_result(result)
    except (ArtifactError, FileSecurityError, OSError, ResolutionError, PlanError, SchemaError) as exc:
        print(f"[s3-upload] config_error: {exc}", file=sys.stderr)
        return 2
    except OperationError as exc:
        print(f"[s3-upload] runtime_error: {exc}", file=sys.stderr)
        return 1


def _existing_result_check(data: bytes) -> None:
    # A pre-existing --result-out destination must be a prior result handoff;
    # anything else is a foreign file this command refuses to clobber.
    try:
        validate_result(json.loads(data.decode("utf-8")), validate_reference=False)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ArtifactError("existing result output is not a prior result") from exc


def _result_out_preflight(path, *, cwd, config_home, source):
    try:
        return preflight_result_output(
            path,
            project_root=cwd,
            config_home=config_home,
            source_identity=(source.snapshot.device, source.snapshot.inode),
            existing_content_check=_existing_result_check,
        )
    except (ArtifactError, FileSecurityError, OSError) as exc:
        raise PlanError(f"result output preflight failed: {exc}") from exc


def _result_payload(result) -> bytes:
    # Byte-for-byte the line stdout --json prints, newline included.
    return (
        json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _write_result_out(snapshot, result) -> bool:
    try:
        atomic_write(snapshot.value["path"], _result_payload(result))
        return True
    except (ArtifactError, FileSecurityError, OSError) as exc:
        print(f"[s3-upload] result_error: {exc}", file=sys.stderr, flush=True)
        return False


def _checkpoint_request_builder(resolved):
    candidate = provider_candidate_for_target(resolved.target)
    return (
        build_signed_request
        if candidate is None
        else partial(build_candidate_request, candidate)
    )


def _emit_checkpoint_outcome(args, outcome) -> int:
    result = outcome.result
    if args.json:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
    elif result["url"] is not None:
        print(result["url"], flush=True)
    else:
        print(
            f"[s3-upload] {result['status']} checkpoint_id={result['checkpoint_id'] or 'none'}",
            file=sys.stderr,
            flush=True,
        )
    outcome.finalize()
    return exit_code_for_result(result)


def _resolve_checkpoint(args, *, environ, cwd, config_home, now):
    execution_mode, live_interlock, allow_candidates = _live_test_context(environ)
    store = CheckpointStore(cwd)
    checkpoint = store.load(args.checkpoint)
    selected_ref = args.target or checkpoint["target_ref"]
    if args.target is None and selected_ref.startswith("global:") and not args.use_local_key:
        raise ResolutionError(
            "Checkpoint does not authorize reading a Global Target; pass --use-local-key"
        )
    resolved = resolve_target(
        cwd=cwd,
        config_home=config_home,
        environ=environ,
        cli_target=selected_ref,
        cli_caller=None,
        use_local_key=args.use_local_key,
        now=now,
        allow_candidates=allow_candidates,
    )
    if args.target is None:
        resolved = replace(resolved, source="checkpoint-original")
    checkpoint = parse_checkpoint(
        checkpoint, credentials=_active_credentials(resolved)
    )
    if resolved.target_fingerprint != checkpoint["target_fingerprint"]:
        raise ResolutionError(
            "Checkpoint Target fingerprint does not match the selected Target"
        )
    if resolved.target.endpoint.startswith("http://") and not args.allow_insecure_http:
        raise ResolutionError("HTTP Target requires --allow-insecure-http")
    return (
        store,
        checkpoint,
        resolved,
        execution_mode,
        live_interlock,
        _checkpoint_request_builder(resolved),
    )


def _v2_resume(args, *, environ, cwd, config_home, transport, now) -> int:
    store = CheckpointStore(cwd)
    try:
        with store.lock(args.checkpoint):
            (
                store,
                checkpoint,
                resolved,
                execution_mode,
                live_interlock,
                request_builder,
            ) = _resolve_checkpoint(
                args, environ=environ, cwd=cwd, config_home=config_home, now=now
            )
            if (
                execution_mode == "test-only"
                and resolved.target.provider in EXPERIMENTAL_PROVIDERS
            ):
                raise ResolutionError(
                    "candidate execution requires the authorized evidence harness"
                )
            outcome = resume_multipart(
                resolved=resolved,
                checkpoint=checkpoint,
                store=store,
                transport=transport,
                project_root=cwd,
                config_home=config_home,
                now=now,
                execution_mode=execution_mode,
                live_test_interlock=live_interlock,
                allow_insecure_http=args.allow_insecure_http,
                request_builder=request_builder,
            )
            return _emit_checkpoint_outcome(args, outcome)
    except SourceError as exc:
        print(f"[s3-upload] file_error: {exc}", file=sys.stderr)
        return 3
    except (
        ArtifactError,
        FileSecurityError,
        OSError,
        ResolutionError,
        MultipartError,
        SchemaError,
    ) as exc:
        print(f"[s3-upload] runtime_error: {exc}", file=sys.stderr)
        return 1


def _v2_abort(args, *, environ, cwd, config_home, transport, now) -> int:
    store = CheckpointStore(cwd)
    try:
        with store.lock(args.checkpoint):
            (
                store,
                checkpoint,
                resolved,
                execution_mode,
                live_interlock,
                request_builder,
            ) = _resolve_checkpoint(
                args, environ=environ, cwd=cwd, config_home=config_home, now=now
            )
            if (
                execution_mode == "test-only"
                and resolved.target.provider in EXPERIMENTAL_PROVIDERS
            ):
                raise ResolutionError(
                    "candidate execution requires the authorized evidence harness"
                )
            outcome = abort_multipart(
                resolved=resolved,
                checkpoint=checkpoint,
                store=store,
                transport=transport,
                confirm_abort=args.confirm_abort,
                now=now,
                execution_mode=execution_mode,
                live_test_interlock=live_interlock,
                allow_insecure_http=args.allow_insecure_http,
                request_builder=request_builder,
            )
            return _emit_checkpoint_outcome(args, outcome)
    except (
        ArtifactError,
        FileSecurityError,
        OSError,
        ResolutionError,
        MultipartError,
        SchemaError,
    ) as exc:
        print(f"[s3-upload] runtime_error: {exc}", file=sys.stderr)
        return 1


def _v2_reconcile(args, *, environ, cwd, config_home, transport, now) -> int:
    store = CheckpointStore(cwd)
    try:
        with store.lock(args.checkpoint):
            (
                store,
                checkpoint,
                resolved,
                execution_mode,
                live_interlock,
                request_builder,
            ) = _resolve_checkpoint(
                args, environ=environ, cwd=cwd, config_home=config_home, now=now
            )
            if (
                execution_mode == "test-only"
                and resolved.target.provider in EXPERIMENTAL_PROVIDERS
            ):
                raise ResolutionError(
                    "candidate execution requires the authorized evidence harness"
                )
            if checkpoint["kind"] == "put":
                outcome = reconcile_put(
                    resolved=resolved, checkpoint=checkpoint, store=store,
                    transport=transport, config_home=config_home, now=now,
                )
            elif checkpoint["kind"] == "delete":
                outcome = reconcile_delete(
                    resolved=resolved, checkpoint=checkpoint, store=store,
                    transport=transport, now=now,
                )
            elif checkpoint["kind"] == "multipart":
                outcome = reconcile_multipart(
                    resolved=resolved,
                    checkpoint=checkpoint,
                    store=store,
                    transport=transport,
                    project_root=cwd,
                    config_home=config_home,
                    now=now,
                    execution_mode=execution_mode,
                    live_test_interlock=live_interlock,
                    allow_insecure_http=args.allow_insecure_http,
                    request_builder=request_builder,
                )
            else:
                raise OperationError("unsupported checkpoint kind")
            return _emit_checkpoint_outcome(args, outcome)
    except (
        ArtifactError,
        FileSecurityError,
        OSError,
        ResolutionError,
        OperationError,
        MultipartError,
        SchemaError,
    ) as exc:
        print(f"[s3-upload] runtime_error: {exc}", file=sys.stderr)
        return 1


def _v2_main(argv, *, environ, cwd, config_home, transport, now) -> int:
    args = v2_parser().parse_args(argv)
    if args.operation == "probe":
        artifact = build_probe(
            cwd=cwd,
            config_home=config_home,
            environ=environ,
            cli_target=args.target,
            cli_caller=args.caller_skill,
            use_local_key=args.use_local_key,
            executable_path=sys.executable,
            state_root=os.path.join(cwd, ".s3-upload"),
        )
        sys.stdout.write(serialize_artifact(artifact).decode("utf-8") + "\n")
        return 0
    if args.operation == "url":
        return _v2_url(args, environ=environ, cwd=cwd, config_home=config_home, now=now)
    if args.operation == "delete":
        return _v2_delete(
            args, environ=environ, cwd=cwd, config_home=config_home,
            transport=transport, now=now,
        )
    if args.operation == "reconcile":
        return _v2_reconcile(
            args, environ=environ, cwd=cwd, config_home=config_home,
            transport=transport, now=now,
        )
    if args.operation == "resume":
        return _v2_resume(
            args, environ=environ, cwd=cwd, config_home=config_home,
            transport=transport, now=now,
        )
    if args.operation == "abort":
        return _v2_abort(
            args, environ=environ, cwd=cwd, config_home=config_home,
            transport=transport, now=now,
        )
    if args.operation != "upload":
        print("[s3-upload] config_error: operation is not available in this implementation stage", file=sys.stderr)
        return 2
    dry_run = None
    try:
        execution_mode, live_interlock, allow_candidates = _live_test_context(environ)
        resolved = resolve_target(
            cwd=cwd,
            config_home=config_home,
            environ=environ,
            cli_target=args.target,
            cli_caller=args.caller_skill,
            use_local_key=args.use_local_key,
            now=now,
            allow_candidates=allow_candidates,
        )
        dry_run = build_upload_dry_run(
            resolved=resolved,
            file_path=args.file,
            explicit_key=args.key,
            content_type=args.content_type,
            cache_control=args.cache_control,
            content_disposition=args.content_disposition,
            presign_expires=args.presign_expires,
            reference_out=args.reference_out,
            project_root=cwd,
            config_home=config_home,
            allow_insecure_http=args.allow_insecure_http,
            now=now,
            execution_mode=execution_mode,
            live_test_interlock=live_interlock,
            collision_override=args.collision,
        )
        # The --result-out destination is preflighted here, after the local
        # plan exists (its source identity guards aliasing) and before any
        # remote request can exist; a rejection therefore leaves zero
        # requests behind.
        result_snapshot = None
        if args.result_out is not None:
            result_snapshot = _result_out_preflight(
                args.result_out, cwd=cwd, config_home=config_home,
                source=dry_run.source,
            )
        if args.dry_run:
            result = build_result(
                "upload",
                "dry_run",
                object_written=False,
                url_kind=dry_run.plan["access"]["url_kind"],
                retention=dry_run.plan["retention"],
                plan=dry_run.plan,
            )
            wrote = result_snapshot is None or _write_result_out(result_snapshot, result)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            else:
                print("[s3-upload] dry_run " + json.dumps(dry_run.plan, ensure_ascii=False, separators=(",", ":")), file=sys.stderr)
            code = exit_code_for_result(result)
            return code if wrote or code != 0 else 1
        if not dry_run.executable:
            if result_snapshot is not None:
                # Rejected before any request: the handoff file still gets
                # the full nine-field result, with every inapplicable value
                # an explicit null.
                _write_result_out(
                    result_snapshot,
                    build_result(
                        "upload", "not_started", object_written=False,
                        retention=dry_run.plan["retention"],
                    ),
                )
            print("[s3-upload] config_error: upload plan is blocked", file=sys.stderr)
            return 2
        if (
            execution_mode == "test-only"
            and resolved.target.provider in EXPERIMENTAL_PROVIDERS
        ):
            raise PlanError("candidate execution requires the authorized evidence harness")
        if dry_run.plan["upload_mode"] == "multipart":
            outcome = execute_multipart(
                resolved=resolved,
                plan=dry_run.plan,
                transport=transport,
                project_root=cwd,
                config_home=config_home,
                now=now,
                checkpoint_notice=lambda value: print(
                    f"[s3-upload] checkpoint_id={value}", file=sys.stderr, flush=True
                ),
                execution_mode=execution_mode,
                live_test_interlock=live_interlock,
                allow_insecure_http=args.allow_insecure_http,
                request_builder=_checkpoint_request_builder(resolved),
                source=dry_run.source,
            )
        else:
            outcome = execute_single_put(
                resolved=resolved,
                plan=dry_run.plan,
                transport=transport,
                project_root=cwd,
                config_home=config_home,
                now=now,
                checkpoint_notice=lambda value: print(
                    f"[s3-upload] checkpoint_id={value}", file=sys.stderr, flush=True
                ),
                source=dry_run.source,
            )
        result = outcome.result
        wrote = result_snapshot is None or _write_result_out(result_snapshot, result)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, separators=(",", ":")), flush=True)
        elif result["url"] is not None:
            print(result["url"], flush=True)
        if result["status"] != "ok":
            print(
                f"[s3-upload] {result['status']} checkpoint_id={outcome.checkpoint_id if outcome.retain_checkpoint else 'none'}",
                file=sys.stderr,
                flush=True,
            )
        outcome.finalize()
        code = exit_code_for_result(result)
        return code if wrote or code != 0 else 1
    except (LocalFileError, SourceError) as exc:
        print(f"[s3-upload] file_error: {exc}", file=sys.stderr)
        return 3
    except (ResolutionError, PlanError) as exc:
        print(f"[s3-upload] config_error: {exc}", file=sys.stderr)
        return 2
    except (OperationError, MultipartError) as exc:
        print(f"[s3-upload] runtime_error: {exc}", file=sys.stderr)
        return 1
    finally:
        if dry_run is not None:
            dry_run.close()

def main(argv=None, *, environ=None, cwd=None, config_home=None, transport=http_request, now=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    environ = dict(os.environ if environ is None else environ)
    cwd = cwd or os.getcwd()
    config_home = config_home or os.path.expanduser("~/.config/s3-upload")
    commands = {"upload", "url", "delete", "resume", "reconcile", "abort", "probe"}
    if argv and argv[0] in commands | {"-h", "--help"}:
        return _v2_main(
            argv,
            environ=environ,
            cwd=cwd,
            config_home=config_home,
            transport=transport,
            now=now,
        )
    print(
        "[s3-upload] config_error: v1 flat configuration is no longer accepted; "
        "choose project/global scope and follow references/configuration.md",
        file=sys.stderr,
    )
    return 2

if __name__ == "__main__": raise SystemExit(main())
