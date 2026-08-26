import re
from pathlib import Path

import tomllib

from video_to_markdown import __version__


def test_version_is_consistent():
    skill_dir = Path(__file__).resolve().parents[1]
    pyproject_version = tomllib.loads((skill_dir / "pyproject.toml").read_text())["project"][
        "version"
    ]
    skill_text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    frontmatter_version = re.search(r"^version:\s*(\S+)$", skill_text, re.MULTILINE).group(1)
    description_version = re.search(
        r"^description:\s*v([^｜]+)｜", skill_text, re.MULTILINE
    ).group(1)
    assert __version__ == pyproject_version == frontmatter_version == description_version
