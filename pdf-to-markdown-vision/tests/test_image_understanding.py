"""Public workflow and CommonMark checks; host vision is verified separately."""
import json
from pathlib import Path

from markdown_it import MarkdownIt
import pytest


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("before,caption,after", [
    ("", "", ""),
    ("Source paragraph.", "Figure 1. Source caption.", "Following source paragraph."),
    ("- Previous item\n- Another item", "Source caption.", "1. Next item\n2. Another item"),
])
def test_understanding_has_rendered_boundaries_and_survives_cleaning(pdf, command, tmp_path, before, caption, after):
    profile = tmp_path / "profile.md"
    profile.write_text("Remove only the Vendor header; preserve illustrations, captions and explanations.", encoding="utf-8")
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "host",
                    "--vision", "available", "--cleaning", profile)
    task, writer = Path(start["task_dir"]), start["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    blocks = [{"id": "header", "bbox": [0, 0, 1, .1], "markdown": "Vendor header",
               "rules": ["reading-order"], "reason": "Independent source header"}]
    if before:
        blocks.append({"id": "before", "bbox": [0, .1, 1, .2], "markdown": before,
                       "rules": ["reading-order"], "reason": "Independent preceding content"})
    blocks.append({"id": "figure", "bbox": [0, .2, 1, .8], "image": True, "alt": "Instrument view",
                   "markdown": caption, "image_understanding": "The screen shows the instrument view.\n\nIts panels are adjacent.",
                   "rules": ["visual-unit"], "reason": "Observed object and panel relationship"})
    if after:
        blocks.append({"id": "after", "bbox": [0, .8, 1, 1], "markdown": after,
                       "rules": ["reading-order"], "reason": "Independent following content"})
    page = command("submit-page", "--task", task, "--writer", writer, "--page", 1,
                   "--input", write(tmp_path / "page.json", {"review": "Synthetic contract check", "blocks": blocks}))
    draft = command("finish", "--task", task, "--writer", writer)
    raw = (task / draft["input_dir"] / "raw.md").read_text(encoding="utf-8")
    cleaned = raw.removeprefix("Vendor header\n\n")
    command("submit-cleaning", "--task", task, "--writer", writer, "--input", write(tmp_path / "clean.json", {
        "markdown": cleaned, "review": "Removed only the selected header", "changes": [
            {"page": 1, "block": "header", "action": "remove", "reason": "Exact profile match"}]}))
    final = command("finish", "--task", task, "--writer", writer)
    assert final["status"] == "complete"
    folder = task / final["current_result"]
    provenance = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    for path in (task / page["path"] / "raw.md", folder / "raw.md", folder / "cleaned.md", folder / "document.md"):
        text = path.read_text(encoding="utf-8")
        quotes = [t for t in MarkdownIt("commonmark").parse(text) if t.type == "blockquote_open"]
        assert len(quotes) == 1 and quotes[0].level == 0
        first, end = quotes[0].map
        lines = text.splitlines()
        assert lines[first - 1] == lines[end] == ""
        assert lines[first:first + 2] == ["> **图片理解**", ">"]
        assert all(value.startswith(">") for value in lines[first:end])
        assert caption in text and before in text and after in text
        if not after:
            assert text.endswith("\n\n")
    for name in ("raw.md", "cleaned.md", "document.md"):
        lines = (folder / name).read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines, 1):
            if line.startswith(">"):
                association = next(r for r in provenance["outputs"][name] if r["line_start"] <= index <= r["line_end"])
                assert association["sources"] == [{"page": 1, "block": "figure", "kind": "image-understanding"}]
    figure = next(b for b in provenance["pages"][0]["blocks"] if b["id"] == "figure")
    assert {span["kind"] for span in figure["content_spans"]} >= {"image", "image-understanding"}


@pytest.mark.parametrize("understanding", [None, "", " \n "])
def test_uninterpretable_illustration_can_complete_without_placeholder(pdf, command, tmp_path, understanding):
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "host", "--vision", "available")
    task, writer = Path(start["task_dir"]), start["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    block = {"id": "figure", "bbox": [0, 0, 1, 1], "image": True, "alt": "Unidentified illustration",
             "markdown": "", "rules": ["visual-unit"], "reason": "Overall meaning cannot be reliably understood"}
    if understanding is not None:
        block["image_understanding"] = understanding
    command("submit-page", "--task", task, "--writer", writer, "--page", 1,
            "--input", write(tmp_path / "page.json", {"review": "Image preserved; no reliable explanation", "blocks": [block]}))
    final = command("finish", "--task", task, "--writer", writer)
    assert final["status"] == "complete"
    text = (task / final["current_result"] / "document.md").read_text(encoding="utf-8")
    assert text.startswith("![") and len(text.splitlines()) == 1


def test_cleaning_cannot_strip_the_final_quote_boundary(pdf, command, tmp_path):
    profile = tmp_path / "profile.md"
    profile.write_text("Keep everything.", encoding="utf-8")
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "host",
                    "--vision", "available", "--cleaning", profile)
    task, writer = Path(start["task_dir"]), start["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    block = {"id": "figure", "bbox": [0, 0, 1, 1], "image": True, "markdown": "",
             "image_understanding": "Instrument view.", "rules": ["visual-unit"], "reason": "Visible instrument"}
    command("submit-page", "--task", task, "--writer", writer, "--page", 1,
            "--input", write(tmp_path / "page.json", {"review": "Contract check", "blocks": [block]}))
    draft = command("finish", "--task", task, "--writer", writer)
    raw = (task / draft["input_dir"] / "raw.md").read_text(encoding="utf-8")
    error = command("submit-cleaning", "--task", task, "--writer", writer, "--input", write(tmp_path / "clean.json", {
        "markdown": raw.rstrip() + "\n", "review": "Intentionally damaged boundary", "changes": []}), expect=2)
    assert "blank" in error["error"]


def test_page_join_keeps_source_quote_separate_from_understanding(pdf, command, tmp_path):
    start = command("start", pdf, "--project", tmp_path, "--owner", "host", "--vision", "available")
    task, writer = Path(start["task_dir"]), start["writer"]
    for page in (1, 2):
        command("render", "--task", task, "--writer", writer, "--page", page)
        block = {"id": "content", "bbox": [0, 0, 1, 1], "rules": ["reading-order"],
                 "reason": "Two independent page contents"}
        if page == 1:
            block.update(image=True, markdown="", image_understanding="Observed illustration.")
        else:
            block["markdown"] = "> Independent source quotation."
        command("submit-page", "--task", task, "--writer", writer, "--page", page,
                "--input", write(tmp_path / f"page-{page}.json", {"review": "Synthetic page boundary", "blocks": [block]}))
    final = command("finish", "--task", task, "--writer", writer)
    assert final["status"] == "complete"
    text = (task / final["current_result"] / "document.md").read_text(encoding="utf-8")
    quotes = [token for token in MarkdownIt("commonmark").parse(text) if token.type == "blockquote_open"]
    assert len(quotes) == 2
    lines = text.splitlines()
    assert "Independent source quotation." not in "\n".join(lines[slice(*quotes[0].map)])
    assert lines[quotes[1].map[0]] == "> Independent source quotation."
