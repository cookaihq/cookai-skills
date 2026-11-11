"""Synthetic PDFs exercise the public workflow; they do not test host vision."""
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image, ImageDraw
import pytest

SKILL = Path(__file__).resolve().parents[1]


@pytest.fixture
def pdf(tmp_path):
    images = []
    for page in (1, 2):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        draw.text((40, 40), f"Instrument manual - page {page}", fill="black")
        draw.rectangle((80, 160, 520, 600), outline="black", width=4)
        draw.text((120, 200), "Bandwidth: 100 kHz", fill="black")
        images.append(image)
    source = tmp_path / "manual.pdf"
    images[0].save(source, save_all=True, append_images=images[1:])
    return source


@pytest.fixture
def command(tmp_path):
    def run(*args, expect=0, cwd=None, env=None):
        result = subprocess.run(
            [sys.executable, str(SKILL / "scripts/workflow.py"), *map(str, args)],
            cwd=cwd or tmp_path, env=env or dict(os.environ),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        assert result.returncode == expect, result.stdout + result.stderr
        return json.loads(result.stdout)
    return run


def page_payload(path, text="Bandwidth: 100 kHz", *, issue=False):
    value = {
        "printed_page": "1",
        "review": "Compared visible heading, unit and complete instrument boundary with the rendered page.",
        "blocks": [{
            "id": "instrument", "bbox": [0.1, 0.1, 0.9, 0.9],
            "markdown": text, "image": True, "alt": "Instrument and its labels",
            "rules": ["visual-unit"], "reason": "The label belongs to the pictured instrument.",
        }],
        "issues": ([{"block": "instrument", "message": "A label is unreadable", "blocking": True}] if issue else []),
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path
