from __future__ import annotations

from datetime import datetime, timezone
from contextlib import contextmanager
import json
import hashlib
import errno
import os
from pathlib import Path
import re
import shutil
import uuid
from typing import Any

from filelock import FileLock

from . import config, download, pdf
from .files import SKILL, atomic, ignore_data, read_json, require_space, sha, validate_images, version, within, write_json

RULES = {"reading-order", "visual-unit", "table-groups", "merged-conditions", "formulas"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def locked(task: Path):
    if not task.is_dir():
        raise ValueError("Task directory does not exist")
    return FileLock(str(task / ".write.lock"), timeout=0)


def load(task: Path) -> dict[str, Any]:
    state: dict[str, Any] = {}
    raw = (task / "events.jsonl").read_bytes()
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            state["incomplete_event_tail"] = True
            break
        event = json.loads(line)
        if event["seq"] != state.get("seq", 0) + 1:
            raise ValueError("Event sequence is damaged; inspect evidence before continuing")
        state.update(event["set"])
        for key in ("pages", "renders"):
            state.setdefault(key, {}).update(event.get(key, {}))
        state["seq"] = event["seq"]
    if not state:
        raise ValueError("No committed task history")
    return state


def summary(task: Path, state: dict) -> None:
    write_json(task / "task.json", state)
    run = state.get("run", {})
    pages = state.get("pages", {})
    complete = [int(p) for p, value in pages.items() if value["status"] == "complete"]
    problems = [f"- Physical page {p}: {value['status']} — {value.get('error', '')}"
                for p, value in pages.items() if value["status"] != "complete"]
    text = (f"# {state['name']}\n\nTask ID: `{state['id']}`\n\nStatus: **{state['status']}**\n\n"
            f"Source: [document.pdf](source/document.pdf), SHA-256 `{state['source_hash']}`\n\n"
            f"Physical pages: {run.get('target_pages', [])}; complete: {sorted(complete)}\n\n"
            f"Current result: {state.get('current_result') or 'none'}\n\n"
            f"Writer: {state.get('writer') or 'released'}\n\n"
            f"Latest event: {state['seq']}; Skill: {run.get('skill_version', '')}\n\n"
            + "\n".join(problems) + "\n\n"
            "Resume: load this README, task.json and related events; use the installed Skill's resume command. "
            "Verify source and saved files before reuse. A newer Skill reruns all target pages.\n")
    atomic(task / "README.md", text)


def commit(task: Path, state: dict, action: str, changes=None, *, pages=None, renders=None, details=None) -> None:
    if state.pop("incomplete_event_tail", False):
        raw = (task / "events.jsonl").read_bytes()
        boundary = raw.rfind(b"\n") + 1
        evidence = f"evidence/interrupted-event-{uuid.uuid4().hex}.bin"
        atomic(task / evidence, raw[boundary:])
        atomic(task / "events.jsonl", raw[:boundary])
        commit(task, state, "interrupted-event-preserved", details={"evidence": evidence})
    event = {"seq": state.get("seq", 0) + 1, "time": now(), "action": action,
             "run": state.get("run", {}).get("id"), "set": changes or {},
             "pages": pages or {}, "renders": renders or {}, "details": details or {}}
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    with (task / "events.jsonl").open("ab") as stream:
        stream.write(line)
        stream.flush()
        os.fsync(stream.fileno())
    state.update(event["set"])
    for key in ("pages", "renders"):
        state.setdefault(key, {}).update(event[key])
    state["seq"] = event["seq"]
    summary(task, state)


def snapshot(task: Path, run_id: str, requirements: dict) -> str:
    # Failed or interrupted attempts must never reserve the next committed run ID.
    folder = task / "snapshots" / (run_id + "-" + uuid.uuid4().hex[:12])
    folder.mkdir(parents=True)
    shutil.copyfile(SKILL / "SKILL.md", folder / "SKILL.md")
    shutil.copytree(SKILL / "references", folder / "references")
    write_json(folder / "requirements.json", requirements)
    return folder.relative_to(task).as_posix()


def cleaning_snapshot(task: Path, path: str, selected: dict) -> dict:
    metadata = {k: v for k, v in selected.items() if k != "content"}
    if selected.get("content") is not None:
        relative = path + "/cleaning.md"
        atomic(task / relative, selected["content"])
        metadata.update(path=relative, sha256=sha(task / relative))
    write_json(task / path / "cleaning.json", metadata)
    return metadata


def status(task: Path, state: dict | None = None) -> dict:
    state = state if state is not None else load(task)
    run = state["run"]
    return {"task_dir": str(task), "task_id": state["id"], "status": state["status"],
            "writer": state.get("writer"), "owner": state.get("owner"), "run": run,
            "pending_pages": [p for p in run["target_pages"]
                              if state.get("pages", {}).get(str(p), {}).get("status") != "complete"],
            "current_result": state.get("current_result"), "versions": state.get("versions", []),
            "pages": state.get("pages", {}), "seq": state["seq"]}


def start(source: str, project: Path, pages: str | None, owner: str, vision: str, *, related_task=None,
          track_data=False, **options) -> dict:
    if vision != "available":
        raise ValueError("Native host image viewing is required; no conversion service fallback")
    if not project.is_dir():
        raise ValueError("Explicit project root must be an existing directory")
    require_space(project)
    tracking = "explicit-track" if track_data else ignore_data(project)
    remote = source.startswith(("http://", "https://"))
    attempts = []
    if remote:
        original = project / "data/pdf-to-markdown" / (".download-" + uuid.uuid4().hex) / "document.pdf"
        attempts = download.fetch(source, original)
        origin = download.redacted(source)
        from urllib.parse import urlsplit
        short_name = Path(urlsplit(source).path).stem
    else:
        original = Path(source).expanduser().resolve()
        origin = str(original)
        short_name = original.stem
    count = pdf.inspect(original)
    require_space(project, original.stat().st_size)
    selected = pdf.page_range(pages, count)
    ident = uuid.uuid4().hex[:12]
    name = re.sub(r"[^\w-]", "_", short_name)[:40] or "document"
    task = project / "data/pdf-to-markdown" / (datetime.now().strftime("%Y%m%d-%H%M%S") + f"_{name}_{ident}")
    task.mkdir(parents=True)
    (task / "source").mkdir()
    shutil.copyfile(original, task / "source/document.pdf")
    state: dict[str, Any] = {}
    run: dict[str, Any] = {"id": "r001", "skill_version": version(), "target_pages": selected, "started_at": now()}
    run["snapshot"] = snapshot(task, run["id"], {"pages": selected, "cleaning": None})
    run["cleaning"] = cleaning_snapshot(task, run["snapshot"], config.select(project, **options))
    with locked(task):
        commit(task, state, "task-started", {"id": ident, "name": name, "schema": 1,
               "source_hash": sha(task / "source/document.pdf"), "source_origin": origin,
               "related_task_id": load(Path(related_task).resolve())["id"] if related_task else None,
               "git_data": tracking, "download_attempts": attempts,
               "page_count": count, "run": run, "run_count": 1, "status": "in-progress",
               "writer": uuid.uuid4().hex, "owner": owner, "versions": [], "current_result": None})
    if remote:
        original.unlink()
        original.parent.rmdir()
    return status(task, state)


def require_writer(state: dict, writer: str) -> None:
    if not writer or state.get("writer") != writer:
        raise ValueError("Task belongs to another writer: " + str(state.get("owner")) + "; use status/resume")
    if state["run"]["skill_version"] != version():
        raise ValueError("Skill changed; resume with the current version before writing")


@contextmanager
def recording(task: Path, state: dict, action: str, writer: str | None):
    authorized = bool(writer and writer == state.get("writer"))
    try:
        if authorized and action not in {"pause", "fail-page", "block-cleaning"}:
            require_space(task)
        if authorized and sha(task / "source/document.pdf") != state["source_hash"]:
            raise ValueError("Saved source content changed; start a related task, never overwrite its history")
        yield
    except Exception as exc:
        if authorized:
            try:
                current = load(task)
                changes = {"status": "paused-space", "writer": None} if isinstance(exc, OSError) and exc.errno == errno.ENOSPC else {}
                commit(task, current, "attempt-failed", changes, details={"operation": action, "kind": type(exc).__name__, "error": str(exc)})
            except OSError:
                pass  # Existing committed events remain authoritative when the disk cannot accept a failure event.
        raise


def require_page(state: dict, page: int) -> None:
    if page not in state["run"]["target_pages"]:
        raise ValueError("Page is outside the current physical page range")


def resume(task: Path, state: dict, owner: str, vision: str, *, writer=None, takeover=False,
           pages=None, redo_pages=None, project=None, **options) -> dict:
    if vision != "available":
        raise ValueError("Native host image viewing is required")
    if state.get("writer") and state["writer"] != writer and not takeover:
        raise ValueError("Task already owned by " + str(state.get("owner")) + "; pause that writer or explicitly take over after it has stopped")
    if sha(task / "source/document.pdf") != state["source_hash"]:
        raise ValueError("Saved source changed; keep this history and start a related task for new source content")
    selected = pdf.page_range(pages, state["page_count"]) if pages else state["run"]["target_pages"]
    redo = set(pdf.page_range(redo_pages, state["page_count"])) if redo_pages else set()
    upgraded = state["run"]["skill_version"] != version()
    reusable = {}
    invalid = []
    for page in selected:
        entry = state.get("pages", {}).get(str(page))
        if not entry or page in redo or upgraded:
            continue
        if entry.get("files") and all(within(task, name).is_file() and sha(within(task, name)) == digest
                                      for name, digest in entry["files"].items()):
            reusable[str(page)] = entry
        else:
            invalid.append(page)
    if any(options.values()):
        root = project or (task.parents[2] if task.parent.name == "pdf-to-markdown" and task.parent.parent.name == "data" else None)
        if root is None and not options.get("no_cleaning"):
            raise ValueError("Pass the selected project root to resolve cleaning configuration")
        chosen = config.select(root or task, **options)
    else:
        chosen = dict(state["run"].get("cleaning", {"enabled": False}))
        if chosen.get("path"):
            chosen["content"] = within(task, chosen["path"]).read_text(encoding="utf-8")
    run_id = f"r{state['run_count'] + 1:03d}"
    run: dict[str, Any] = {"id": run_id, "skill_version": version(), "target_pages": selected, "started_at": now(),
           "snapshot": snapshot(task, run_id, {"pages": selected, "cleaning": None})}
    run["cleaning"] = cleaning_snapshot(task, run["snapshot"], chosen)
    commit(task, state, "run-resumed", {"run": run, "run_count": state["run_count"] + 1,
           "pages": reusable, "renders": {}, "status": "in-progress", "writer": uuid.uuid4().hex,
           "owner": owner}, details={"upgraded": upgraded, "invalid_pages": invalid,
                                    "takeover": bool(takeover), "reused_pages": sorted(map(int, reusable))})
    return status(task, state)


def fail_page(task: Path, state: dict, page: int, reason: str) -> dict:
    require_page(state, page)
    if not reason.strip():
        raise ValueError("Failure needs an observable reason")
    commit(task, state, "page-failed", pages={str(page): {"status": "failed", "error": reason,
           "run": state["run"]["id"]}}, details={"page": page, "reason": reason})
    return status(task, state)


def pause(task: Path, state: dict, reason: str) -> dict:
    commit(task, state, "run-paused", {"writer": None, "status": "paused"}, details={"reason": reason})
    return status(task, state)


def adopt(task: Path, state: dict, result: str, owner: str) -> dict:
    if state.get("writer"):
        raise ValueError("Pause the active writer before selecting a historical result")
    path = "results/" + result
    if path not in state["versions"]:
        raise ValueError("Unknown committed result version")
    folder = within(task, path)
    record = read_json(folder / "result.json")
    if record["status"] != "complete":
        raise ValueError("A partial result cannot become the current complete result")
    validate_images(folder, (folder / "document.md").read_text(encoding="utf-8"))
    commit(task, state, "result-adopted", {"current_result": path}, details={"owner": owner, "result": path})
    return status(task, state)


def submit_cleaning(task: Path, state: dict, payload: dict) -> dict:
    cleaning = state["run"].get("cleaning", {})
    if not cleaning.get("enabled") or cleaning.get("error"):
        raise ValueError("This run has no usable selected cleaning requirements")
    draft = state.get("cleaning_input")
    if not draft or draft["run"] != state["run"]["id"]:
        raise ValueError("Call finish to prepare the current uncleaned input first")
    markdown = payload.get("markdown")
    if not isinstance(markdown, str) or not markdown.strip() or not payload.get("review"):
        raise ValueError("Cleaning requires Markdown and actual review facts")
    if not isinstance(payload.get("changes"), list):
        raise ValueError("Record cleaning changes, including an empty list for a reviewed no-op")
    for change in payload["changes"]:
        page = state["pages"].get(str(change.get("page")), {})
        if "path" not in page:
            raise ValueError("Cleaning change refers to a missing page")
        ids = [b["id"] for b in read_json(task / page["path"] / "provenance.json")["blocks"]]
        if change.get("block") not in ids or not change.get("action") or not change.get("reason"):
            raise ValueError("Each cleaning change needs its source page/block, action and reason")
    validate_images(task / draft["path"], markdown)
    relative = f"pages/{state['run']['id']}/cleaning-{uuid.uuid4().hex[:10]}.json"
    write_json(task / relative, payload)
    commit(task, state, "cleaning-saved", {"cleaned": {"path": relative, "sha256": sha(task / relative),
           "input_hash": draft["hash"], "run": state["run"]["id"]}}, details={"changes": payload["changes"], "review": payload["review"]})
    return status(task, state)


def block_cleaning(task: Path, state: dict, reason: str) -> dict:
    run = dict(state["run"])
    run["cleaning"] = {**run.get("cleaning", {}), "error": reason}
    commit(task, state, "cleaning-blocked", {"run": run}, details={"reason": reason})
    return status(task, state)


def render_page(task: Path, state: dict, page: int, dpi=180, crop=None, reason=None) -> dict:
    require_page(state, page)
    image = f"cache/{state['run']['id']}/p{page:04d}-{uuid.uuid4().hex[:8]}.png"
    info = pdf.render(task / "source/document.pdf", page, task / image, dpi, crop)
    text = image.removesuffix(".png") + ".txt"
    atomic(task / text, info.pop("text_layer"))
    commit(task, state, "page-rendered", renders={str(page): {"image": image, "text_layer": text, **info}},
           details={"page": page, "dpi": dpi, "crop": crop, "reason": reason, "cache": True})
    return {"image": image, "text_layer": text, **info}


def submit_page(task: Path, state: dict, page: int, payload: dict) -> dict:
    require_page(state, page)
    if str(page) not in state.get("renders", {}):
        raise ValueError("Render and view the physical page before submitting observations")
    if not isinstance(payload.get("review"), str) or not payload["review"].strip():
        raise ValueError("Provide actual visual review facts")
    blocks = payload.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("A page requires nonempty ordered blocks; use fail-page for failure")
    ids = set()
    for block in blocks:
        key = block.get("id", "")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", key) or key in ids:
            raise ValueError("Block IDs must be unique portable names")
        ids.add(key)
        pdf.bbox(block.get("bbox"))
        rules = block.get("rules")
        if not isinstance(rules, list) or not rules or any(rule not in RULES for rule in rules):
            raise ValueError("Each block must cite known rule IDs")
        if not isinstance(block.get("reason"), str) or not block["reason"].strip():
            raise ValueError("Each block needs its actual rule-selection reason")
        if not isinstance(block.get("markdown"), str) or not block["markdown"].strip():
            raise ValueError("Each block needs content or an explicit unreadable-region note")
    for issue in payload.get("issues", []):
        if issue.get("block") not in ids or not issue.get("message") or type(issue.get("blocking")) is not bool:
            raise ValueError("Issues need a known block, message and boolean blocking flag")
    path = f"pages/{state['run']['id']}/p{page:04d}/{uuid.uuid4().hex[:12]}"
    folder = task / path
    folder.mkdir(parents=True)
    markdown_parts, mappings = [], []
    line = 1
    for block in blocks:
        content = block["markdown"].strip()
        if block.get("image"):
            ref = f"assets/p{page:04d}-{block['id']}.png"
            pdf.render(task / "source/document.pdf", page, folder / ref, 220, block["bbox"])
            alt = block.get("alt", block["id"]).replace("[", "").replace("]", "").replace("\n", " ")
            content = f"![{alt}]({ref})\n\n" + content
        validate_images(folder, content)
        count = len(content.splitlines())
        mappings.append({**block, "line_start": line, "line_end": line + count - 1,
                         "snapshot": state["run"]["snapshot"]})
        markdown_parts.append(content)
        line += count + 1
    raw = "\n\n".join(markdown_parts) + "\n"
    atomic(folder / "raw.md", raw)
    evidence = []
    for issue in payload.get("issues", []):
        block = next(b for b in blocks if b["id"] == issue["block"])
        ref = f"evidence/{state['run']['id']}-p{page:04d}-{uuid.uuid4().hex[:10]}.png"
        pdf.render(task / "source/document.pdf", page, task / ref, 300, block["bbox"])
        evidence.append({**issue, "image": ref})
    record = {"physical_page": page, "printed_page": payload.get("printed_page"), "blocks": mappings,
              "review": payload["review"], "issues": evidence}
    write_json(folder / "provenance.json", record)
    files = {p.relative_to(task).as_posix(): sha(p) for p in folder.rglob("*") if p.is_file()}
    entry = {"path": path, "files": files, "status": "review" if any(i["blocking"] for i in evidence) else "complete",
             "issues": evidence, "run": state["run"]["id"]}
    commit(task, state, "page-saved", pages={str(page): entry}, details={"page": page, "review": payload["review"]})
    return entry


def finish(task: Path, state: dict) -> dict:
    target = state["run"]["target_pages"]
    missing = [p for p in target if state.get("pages", {}).get(str(p), {}).get("status") != "complete"]
    folder = task / "results" / (".building-" + uuid.uuid4().hex)
    folder.mkdir(parents=True)
    texts, provenance = [], []
    line = 1
    for page in target:
        entry = state.get("pages", {}).get(str(page), {})
        if "path" not in entry:
            texts.append(f"> Physical page {page}: missing or failed.\n")
            line += 2
            continue
        source = within(task, entry["path"])
        for rel, expected in entry["files"].items():
            if not within(task, rel).is_file() or sha(within(task, rel)) != expected:
                raise ValueError(f"Saved page {page} is incomplete; resume to verify and repair")
        content = (source / "raw.md").read_text(encoding="utf-8")
        texts.append(content)
        if (source / "assets").exists():
            shutil.copytree(source / "assets", folder / "assets", dirs_exist_ok=True)
        mapping = read_json(source / "provenance.json")
        mapping["document_line_start"] = line
        provenance.append(mapping)
        line += len(content.splitlines()) + 1
    raw = "\n".join(texts)
    validate_images(folder, raw)
    atomic(folder / "raw.md", raw)
    cleaning = state["run"].get("cleaning", {})
    cleaned = state.get("cleaned", {})
    input_hash = hashlib.sha256((raw + json.dumps({p.relative_to(folder).as_posix(): sha(p)
                               for p in sorted(folder.rglob("*.png"))}, sort_keys=True)).encode()).hexdigest()
    usable = cleaned.get("run") == state["run"]["id"] and cleaned.get("input_hash") == input_hash
    if cleaning.get("enabled") and not cleaning.get("error") and not usable:
        input_dir = f"pages/{state['run']['id']}/cleaning-input-{uuid.uuid4().hex[:8]}"
        folder.rename(task / input_dir)
        commit(task, state, "cleaning-input-prepared", {"cleaning_input": {"path": input_dir, "hash": input_hash,
               "run": state["run"]["id"]}}, details={"path": input_dir})
        return {"status": "needs-cleaning", "task_dir": str(task), "writer": state["writer"],
                "input_dir": input_dir, "cleaning": cleaning, "missing_pages": missing}
    document = raw
    if cleaning.get("enabled") and not cleaning.get("error") and usable:
        saved = within(task, cleaned["path"])
        if sha(saved) != cleaned["sha256"]:
            raise ValueError("Saved cleaning output is damaged; submit again after review")
        payload = read_json(saved)
        document = payload["markdown"]
        validate_images(folder, document)
        atomic(folder / "cleaned.md", document)
        write_json(folder / "cleaning-changes.json", payload["changes"])
    atomic(folder / "document.md", document)
    write_json(folder / "provenance.json", {"source_hash": state["source_hash"], "pages": provenance})
    complete = not missing and not cleaning.get("error")
    write_json(folder / "result.json", {"status": "complete" if complete else "partial", "missing_pages": missing,
               "physical_pages": target, "run": state["run"]["id"], "skill_version": version(), "usage": None,
               "cleaning": cleaning, "cleaning_error": cleaning.get("error"),
               "elapsed_seconds": (datetime.now(timezone.utc) - datetime.fromisoformat(state["run"]["started_at"])).total_seconds()})
    signature = hashlib.sha256(json.dumps({"pages": target, "missing": missing, "cleaning_error": cleaning.get("error"),
                  "files": {p.relative_to(folder).as_posix(): sha(p) for p in sorted(folder.rglob("*"))
                            if p.is_file() and (p.suffix == ".png" or p.name in {"raw.md", "document.md"})}},
                  sort_keys=True).encode()).hexdigest()
    old = next((p for p in state["versions"] if read_json(task / p / "result.json").get("signature") == signature), None)
    if old:
        shutil.rmtree(folder)
        result_path = old
    else:
        ordinal = max([int(p.name[1:]) for p in (task / "results").iterdir()
                       if re.fullmatch(r"v\d+", p.name)] + [0]) + 1
        destination = task / "results" / f"v{ordinal:03d}"
        record = read_json(folder / "result.json")
        record["signature"] = signature
        write_json(folder / "result.json", record)
        folder.rename(destination)
        result_path = destination.relative_to(task).as_posix()
    commit(task, state, "result-unchanged" if old else "result-built", {"status": "complete" if complete else "partial",
           "versions": state["versions"] if old else state["versions"] + [result_path], "last_result": result_path,
           "current_result": result_path if complete else state["current_result"], "writer": None},
           details={"missing_pages": missing, "result": result_path})
    if complete:
        cache = task / "cache"
        if cache.exists():
            for path in cache.rglob("*"):
                if path.is_file():
                    path.unlink()
        commit(task, state, "cache-cleaned", details={"scope": "regenerable page renders; evidence and results retained"})
    return status(task, state)
