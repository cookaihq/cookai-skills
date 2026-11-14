from pathlib import Path


def build(project):
    import skill_ui.server
    from video_to_markdown.ui_adapter import build as build_adapter
    # Custom view stays outside the unmodified, hash-checked shared core.
    skill_ui.server.STATIC = Path(__file__).resolve().parents[1] / "ui"
    return build_adapter(project)
