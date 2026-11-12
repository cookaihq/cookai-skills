import json
from pathlib import Path
import shutil

from markdown_it import MarkdownIt
import pytest

from conftest import page_payload


def test_path_input_produces_self_contained_source_images_and_provenance(pdf, command, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    started = command("start", pdf, "--project", project, "--pages", "1", "--owner", "test-host", "--vision", "available")
    task = Path(started["task_dir"])
    writer = started["writer"]
    assert task.parent == project / "data/pdf-to-markdown"
    assert (task / "source/document.pdf").read_bytes() == pdf.read_bytes()
    assert started["pending_pages"] == [1]
    rendered = command("render", "--task", task, "--writer", writer, "--page", 1)
    assert (task / rendered["image"]).is_file()
    command("submit-page", "--task", task, "--writer", writer, "--page", 1,
            "--input", page_payload(tmp_path / "page.json"))
    result = command("finish", "--task", task, "--writer", writer)
    assert result["status"] == "complete"
    assert result["current_result"] == "results/v001"
    standalone = tmp_path / "standalone"
    shutil.copytree(task / result["current_result"], standalone)
    assert "100 kHz" in (standalone / "document.md").read_text(encoding="utf-8")
    assert list((standalone / "assets").glob("*.png"))
    provenance = json.loads((standalone / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["pages"][0]["physical_page"] == 1
    assert provenance["pages"][0]["blocks"][0]["rules"] == ["visual-unit"]
    assert (standalone / "raw.md").read_bytes() == (standalone / "document.md").read_bytes()
    assert not list((task / "cache").rglob("*.png"))
    assert "complete" in (task / "README.md").read_text(encoding="utf-8")


def test_region_line_ranges_point_to_the_actual_output(pdf, command, tmp_path):
    start = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task, writer = Path(start["task_dir"]), start["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    payload = {"review": "Compared two ordered paragraphs", "printed_page": "1", "issues": [],
               "blocks": [{"id": key, "bbox": bounds, "markdown": text, "image": False,
                           "rules": ["reading-order"], "reason": "Paragraph order on the source"}
                          for key, bounds, text in [("heading", [0, 0, 1, .1], "# Manual"),
                                                    ("body", [0, .1, 1, .9], "Parameter: 100 kHz\n\nSecond paragraph")]]}
    path = tmp_path / "page.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    entry = command("submit-page", "--task", task, "--writer", writer, "--page", 1, "--input", path)
    folder = task / entry["path"]
    lines = (folder / "raw.md").read_text(encoding="utf-8").splitlines()
    data = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    for block in data["blocks"]:
        assert "\n".join(lines[block["line_start"] - 1:block["line_end"]]) == block["markdown"]


@pytest.mark.parametrize("caption", ["", "Figure 1. Original caption; range 1 V/div."])
def test_figure_alt_is_literal_searchable_text_with_optional_caption(pdf, command, tmp_path, caption):
    started = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task, writer = Path(started["task_dir"]), started["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    path = page_payload(tmp_path / "page.json", caption)
    payload = json.loads(path.read_text(encoding="utf-8"))
    alt = r"Sweep [1]; V_in (V); path \a; <limit>; *literal*; gain &lt; 1; 2 &amp; 3"
    payload["blocks"][0]["alt"] = alt
    payload["blocks"].append({"id": "body", "bbox": [0, .9, 1, 1], "markdown": "Independent paragraph.",
                              "image": False, "rules": ["reading-order"], "reason": "Outside the visual unit."})
    path.write_text(json.dumps(payload), encoding="utf-8")
    command("submit-page", "--task", task, "--writer", writer, "--page", 1, "--input", path)
    result = command("finish", "--task", task, "--writer", writer)
    assert result["status"] == "complete"
    folder = task / result["current_result"]
    document = (folder / "document.md").read_text(encoding="utf-8")
    images = [child for token in MarkdownIt().parse(document) for child in token.children or [] if child.type == "image"]
    assert len(images) == 1
    assert "".join(child.content for child in images[0].children or []) == alt
    assert (folder / images[0].attrGet("src")).is_file()
    lines = document.splitlines()
    assert lines[0].startswith("![")
    assert lines[2:] == ([caption, "", "Independent paragraph."] if caption else ["Independent paragraph."])
    mappings = json.loads((folder / "provenance.json").read_text(encoding="utf-8"))
    blocks = mappings["pages"][0]["blocks"]
    assert blocks[0]["alt"] == alt and blocks[0]["markdown"] == caption
    assert lines[blocks[1]["line_start"] - 1:blocks[1]["line_end"]] == ["Independent paragraph."]
    assert blocks[0]["line_end"] == (3 if caption else 1)


def test_empty_text_region_still_requires_content(pdf, command, tmp_path):
    started = command("start", pdf, "--project", tmp_path, "--pages", "1", "--owner", "a", "--vision", "available")
    task, writer = Path(started["task_dir"]), started["writer"]
    command("render", "--task", task, "--writer", writer, "--page", 1)
    path = page_payload(tmp_path / "page.json", "")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["blocks"][0]["image"] = False
    path.write_text(json.dumps(payload), encoding="utf-8")
    command("submit-page", "--task", task, "--writer", writer, "--page", 1, "--input", path, expect=2)
    state = command("status", "--task", task)
    assert state["pending_pages"] == [1] and state["current_result"] is None
