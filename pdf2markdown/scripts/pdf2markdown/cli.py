"""One local job per PDF; only the Agent supplies visual review decisions."""
import argparse
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

import aihub_upload
import config
import pdf_source
import settings
from . import assets, export, fsatomic, jcs, network, pipeline, review, sourcefreeze, zipsafe

SCHEMA = "pdf2markdown.job/v2"
COMMAND_SCHEMA = "pdf2markdown.command-result/v2"
EXIT = {"completed": 0, "ready": 0, "host_action_required": 10, "blocked": 20, "failed": 30}
UPLOAD_TTL_SECONDS = 72 * 3600


class WorkflowError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def preflight(pandoc=None):
    # Actual imports catch broken transitive dependencies, not only missing files.
    for name in ("fitz", "lxml.html", "nh3"):
        try:
            importlib.import_module(name)
        except Exception as exc:
            raise WorkflowError("dependency_missing", "%s import failed (%s: %s)" % (name, type(exc).__name__, exc)) from None
    sourcefreeze._import_fitz()
    return assets.find_pandoc(pandoc)


def save_job(work, job):
    fsatomic.atomic_replace_bytes(str(work / ".state/job.json"), jcs.canonicalize(job), "job checkpoint")


def load_job(work):
    path = work / ".state/job.json"
    if not path.exists():
        raise WorkflowError("unsupported_bundle", "No v2 checkpoint. Complete v1 tasks with pdf2markdown/v1.3.0, or reuse their result ZIP in a new v2 task.")
    fsatomic.check_safe_regular_file(str(path), "job checkpoint")
    job = jcs.loads_strict(path.read_bytes())
    if (not isinstance(job, dict) or job.get("schema_version") != SCHEMA
        or not isinstance(job.get("source_name"), str) or not isinstance(job.get("cwd"), str)
        or not os.path.isabs(job["cwd"]) or not isinstance(job.get("page_findings"), dict)):
        raise WorkflowError("invalid_bundle", "Job checkpoint schema is invalid")
    return job


@contextmanager
def locked(work):
    fsatomic.check_safe_dir(str(work), "work bundle")
    fsatomic.check_safe_dir(str(work / ".state"), "state directory")
    path = work / ".state/lock"
    fd = fsatomic.open_regular(str(path), os.O_RDWR | os.O_CREAT, "job lock")
    try:
        try:
            fsatomic.lock_exclusive(fd)
        except BlockingIOError:
            raise WorkflowError("bundle_locked", "Another process is writing this work bundle") from None
        yield
    finally:
        os.close(fd)


def result(work, outcome, **extra):
    return {"schema_version": COMMAND_SCHEMA, "outcome": outcome,
            "work_bundle": str(work) if work else None, **extra}


def validate_artifacts(pipe, job):
    source = pipe.state["phases"].get("source")
    if not isinstance(source, dict):
        raise WorkflowError("invalid_bundle", "Source checkpoint is missing")
    sourcefreeze.verify_frozen_source(source["frozen_path"], source["sha256"], source["byte_count"])
    expected = [(p["path"], p["sha256"]) for p in source["pages"]]
    expected.append((source["inventory_path"], source["inventory_sha256"]))
    direct = pipe.state["phases"].get("direct")
    if direct:
        expected += [(str(Path(direct["staging"]) / m["path"]), m["sha256"]) for m in direct["members"]]
        expected.append((direct["primary_path"], direct["sha256"]))
    reviewed = pipe.state["phases"].get("review") or {}
    if reviewed.get("verified_path"):
        expected.append((reviewed["verified_path"], reviewed["verified_sha256"]))
    if job.get("archive"):
        expected.append((str(Path(pipe.work_dir) / ".state/result.zip"), job["archive"]["sha256"]))
    root = Path(pipe.work_dir)
    for path, digest in expected:
        candidate = Path(path)
        if not candidate.is_relative_to(root) or candidate.resolve() != candidate:
            raise WorkflowError("integrity_violation", "Artifact path escaped its work bundle")
        if fsatomic.read_file_sha256(path) != digest:
            raise WorkflowError("integrity_violation", "Artifact content changed after checkpoint")


def action(pipe, unit):
    phases = pipe.state["phases"]
    token = jcs.sha256_canonical({"source": phases["source"]["sha256"],
                               "direct": phases["direct"]["sha256"],
                               "unit": unit, "review": phases.get("review")})
    return {"kind": "review_page", "action_id": token, **unit,
            "source_inventory": phases["source"]["inventory_path"],
            "direct_markdown": phases["direct"]["primary_path"],
            "page_assignment": "heuristic; verify against the full PDF and adjacent pages"}


def core_stop(work, out):
    return result(work, out["status"], reason=out["reason"])


def credential_for(job, use_local_key):
    kwargs = {"environ": dict(os.environ), "config_home": Path.home() / ".config/pdf2markdown",
              "use_local_key": use_local_key}
    if job.get("credential"):
        return config.read_exact_api_key(job["credential"], **kwargs)
    return config.resolve_api_key(cwd=Path(job["cwd"]), **kwargs)


def drive(work, job, pipe, args, services):
    validate_artifacts(pipe, job)
    source = pipe.state["phases"]["source"]
    archive = work / ".state/result.zip"
    if not pipe.state["phases"].get("direct"):
        if not job.get("archive"):
            key = credential_for(job, args.use_local_key)
            if not job.get("credential"):
                job["credential"] = key.public_identity
                save_job(work, job)
            staged = job.get("staging")
            if not staged:
                # Write the POST intent before any upload, so a crash cannot replay it.
                job["staging"] = {"state": "source_upload_unknown"}
                save_job(work, job)
                fd = fsatomic.open_regular(source["frozen_path"], os.O_RDONLY, "frozen source")
                try:
                    uploaded = aihub_upload.upload_open_source(source_fd=fd, source_sha256=source["sha256"],
                        source_size=source["byte_count"], api_key=key.value, transport=services.get("upload_transport"))
                finally:
                    os.close(fd)
                staged = {"state": uploaded.state, "url": uploaded.url,
                          "expires_at": int(time.time()) + UPLOAD_TTL_SECONDS}
                job["staging"] = staged
                save_job(work, job)
            if staged["state"] != "source_upload_ready":
                return result(work, "blocked", reason={"code": staged["state"],
                    "message": "Upload did not produce a verified URL; resume will not replay the POST."})
            conversion = pipe.state["phases"].get("doc2x") or {}
            if not conversion.get("posted") and int(time.time()) >= staged["expires_at"]:
                return result(work, "blocked", reason={"code": "source_upload_expired", "message": "Staged source expired before task creation."})
            pipe.services["doc2x_transport"] = services.get("doc2x_transport") or network.task_transport(key.value)
            out = pipe.run_doc2x(staged["url"])
            if out["status"] != "artifacts_prepared":
                return core_stop(work, out)
            url = out["doc2x"]["result_url"]
            if archive.exists():
                # This job owns the download path; a completed GET interrupted before
                # checkpointing may be fetched again without creating a paid task.
                fsatomic.check_safe_regular_file(str(archive))
                archive.unlink()
            job["archive"] = network.download(url, archive, zipsafe.ZIP_MAX_COMPRESSED_BYTES,
                                               transport=services.get("download_transport"))
            save_job(work, job)
        out = pipe.adopt_result_zip(str(archive))
        if out["status"] != "artifacts_prepared":
            return core_stop(work, out)
    out = pipe.review_requests()
    if out["status"] == "host_action_requested":
        return result(work, "host_action_required", action=action(pipe, out["request"]))
    if out["status"] != "artifacts_prepared":
        return core_stop(work, out)
    manifest = export.write_result(pipe, job)
    return result(work, "completed", quality=manifest["quality"], warnings=manifest["warnings"],
                  artifacts={name: str(work / "result" / name) for name in
                             ("source.pdf", "direct.md", "final.md", "index.html", "review.json", "manifest.json")})


def validate_review_input(payload, unit):
    if not isinstance(payload, dict) or set(payload) != {"page_complete", "reviews", "page_findings"}:
        raise WorkflowError("review_invalid", "Expected page_complete, reviews, page_findings")
    if payload["page_complete"] is not True or not isinstance(payload["reviews"], list):
        raise WorkflowError("review_invalid", "The Agent must inspect the entire page and return its reviews")
    findings = payload["page_findings"]
    if not isinstance(findings, list) or any(not isinstance(f, str) or not f.strip() for f in findings):
        raise WorkflowError("review_invalid", "page_findings must be a list of nonempty descriptions")
    keys = {"block_id", "verdict", "evidence_page", "evidence_bbox", "replacement_text"}
    for item in payload["reviews"]:
        if not isinstance(item, dict) or set(item) != keys:
            raise WorkflowError("review_invalid", "Each review must contain the five documented fields")
        if not isinstance(item["block_id"], str) or not isinstance(item["verdict"], str):
            raise WorkflowError("review_invalid", "block_id and verdict must be strings")
        if item["verdict"] in review.REPLACEMENT_VERDICTS:
            box = item["evidence_bbox"]
            pages = {unit["page_number"]}
            for side in ("previous_boundary", "next_boundary"):
                if unit.get(side):
                    pages.add(unit[side]["adjacent_page_number"])
            if type(item["evidence_page"]) is not int or item["evidence_page"] not in pages:
                raise WorkflowError("review_invalid", "Correction requires an inspected source page")
            if (not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]):
                raise WorkflowError("review_invalid", "Correction requires a normalized nonempty evidence_bbox")
        elif item["replacement_text"] is not None:
            raise WorkflowError("review_invalid", "Only altered/missing may include replacement_text")
    review.check_coverage(unit, payload["reviews"])


def parser():
    p = argparse.ArgumentParser(description="Convert one PDF and complete page review before exporting Markdown.")
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--source", required=True, help="Local PDF or unauthenticated public HTTPS PDF URL")
    run.add_argument("--output-dir", help="Parent directory for a new, isolated work bundle")
    run.add_argument("--result-zip", help="Reuse an existing Doc2X ZIP; skips upload and task creation")
    run.add_argument("--visual-capability", choices=["available"], required=True)
    for name in ("resume", "inspect", "record-review"):
        child = sub.add_parser(name)
        child.add_argument("--work-bundle", required=True)
        if name == "record-review":
            child.add_argument("--action-id", required=True)
            child.add_argument("--input", required=True)
    for child in sub.choices.values():
        child.add_argument("--pandoc", help="Path to Pandoc 3.10.1")
        child.add_argument("--use-local-key", action="store_true")
    return p


def execute(args, services=None):
    services = dict(services or {})
    work = None
    try:
        if args.command != "run":
            work = Path(os.path.abspath(args.work_bundle))
            # Inspect v1 / malformed bundles before dependency probing; never migrate implicitly.
            fsatomic.check_safe_dir(str(work), "work bundle")
            load_job(work)
        pandoc = None if args.command == "inspect" else preflight(args.pandoc)
        pipe_services = {"pandoc_path": pandoc, "doc2x_base_url": network.ORIGIN,
                         "asset_fetch": services.get("asset_fetch") or
                         (lambda url, limit: network.asset_fetch(url, limit, directory=work / ".state"))}
        pipe_services.update({k: services[k] for k in ("sleep", "monotonic") if k in services})
        if args.command == "run":
            output_dir = args.output_dir or settings.resolve_variable(
                "PDF2MARKDOWN_OUTPUT_DIR", environ=dict(os.environ), cwd=Path.cwd(),
                config_home=Path.home() / ".config/pdf2markdown", use_local_key=args.use_local_key)
            root = Path(os.path.abspath(output_dir or "pdf2markdown-output"))
            root.mkdir(parents=True, exist_ok=True)
            root = root.resolve(strict=True)
            fsatomic.check_safe_dir(str(root), "output root")
            work = Path(tempfile.mkdtemp(prefix="document-", dir=root))
            fsatomic.ensure_private_dir(str(work / ".state"))
            incoming = args.source
            if pdf_source.is_url_input(incoming):
                network.download(incoming, work / ".state/incoming.pdf", sourcefreeze.SOURCE_PDF_MAX_BYTES,
                                 pdf=True, transport=services.get("download_transport"))
                incoming = str(work / ".state/incoming.pdf")
                source_name = "document.pdf"
            else:
                incoming = os.path.abspath(incoming)
                source_name = Path(incoming).name
            job = {"schema_version": SCHEMA, "cwd": str(Path.cwd()), "source_name": source_name,
                   "page_findings": {}}
            with locked(work):
                pipe = pipeline.ConversionPipeline(str(work), pipe_services)
                out = pipe.prepare_source(incoming)
                if out["status"] != "artifacts_prepared":
                    return core_stop(work, out)
                (work / ".state/incoming.pdf").unlink(missing_ok=True)
                if args.result_zip:
                    path = Path(os.path.abspath(args.result_zip))
                    st = fsatomic.check_safe_regular_file(str(path), "result ZIP")
                    if st.st_size > zipsafe.ZIP_MAX_COMPRESSED_BYTES:
                        raise WorkflowError("archive_size_limit", "Result ZIP exceeds byte cap")
                    shutil.copyfile(path, work / ".state/result.zip")
                    os.chmod(work / ".state/result.zip", 0o600)
                    job["archive"] = {"sha256": fsatomic.read_file_sha256(str(work / ".state/result.zip")),
                                      "byte_count": st.st_size}
                save_job(work, job)
                return drive(work, job, pipe, args, services)
        with locked(work):
            job = load_job(work)
            pipe = pipeline.ConversionPipeline(str(work), pipe_services, read_only=args.command == "inspect")
            validate_artifacts(pipe, job)
            if args.command == "inspect":
                phases = pipe.state["phases"]
                return result(work, "ready", phases=list(phases), source_pages=phases["source"]["page_count"],
                              credential_source=(job.get("credential") or {}).get("source_id"),
                              result_dir=str(work / "result") if (work / "result").exists() else None)
            if args.command == "record-review":
                out = pipe.review_requests()
                if out["status"] != "host_action_requested":
                    raise WorkflowError("no_pending_review", "This job has no pending review action")
                expected = action(pipe, out["request"])
                if args.action_id != expected["action_id"]:
                    raise WorkflowError("stale_action", "Review action changed; resume to retrieve the current action")
                path = Path(args.input)
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise WorkflowError("review_invalid", "Review input exceeds byte cap")
                payload = jcs.loads_strict(path.read_bytes())
                validate_review_input(payload, out["request"])
                # The core state owns both the review and page findings, atomically.
                out = pipe.record_review_response(out["request"], payload["reviews"], page_findings=payload["page_findings"])
                if out["status"] != "artifacts_prepared":
                    return core_stop(work, out)
            job["page_findings"] = (pipe.state["phases"].get("review") or {}).get("page_findings", {})
            return drive(work, job, pipe, args, services)
    except (WorkflowError, config.ConfigError, settings.SettingsError, network.DownloadError, pdf_source.PdfSourceError,
            sourcefreeze.SourceFreezeError, assets.AssetError, review.ReviewIncomplete) as exc:
        return result(work, "blocked", reason={"code": getattr(exc, "code", type(exc).__name__), "message": str(exc)})
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        # Broken checkpoint shapes and filesystem failures must not leak raw URLs/keys.
        return result(work, "failed", reason={"code": "invalid_bundle_or_input", "message": type(exc).__name__})


def main(argv=None, services=None):
    args = parser().parse_args(argv)
    # PyMuPDF may print layout recommendations during table extraction. Keep
    # diagnostics off the command protocol, which must be exactly one JSON.
    with redirect_stdout(sys.stderr):
        out = execute(args, services=services)
    sys.stdout.write(json.dumps(out, ensure_ascii=False, allow_nan=False) + "\n")
    return EXIT[out["outcome"]]
