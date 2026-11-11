import json
from pathlib import Path
import shutil

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
