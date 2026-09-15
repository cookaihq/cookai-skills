import re
from pathlib import Path


def test_skill_version_matches_runtime_metadata():
    root = Path(__file__).resolve().parents[1]
    text = (root / "SKILL.md").read_text(encoding="utf-8")
    metadata = (root / "pyproject.toml").read_text(encoding="utf-8")
    version = re.search(r'^version = "([^\"]+)"', metadata, re.MULTILINE).group(1)
    assert re.search(r"^version: (\S+)$", text, re.MULTILINE).group(1) == version
    assert re.search(r"^description: v([^｜]+)｜", text, re.MULTILINE).group(1) == version
