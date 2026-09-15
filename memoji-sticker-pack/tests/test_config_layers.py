import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("layered_skill_config", Path(__file__).resolve().parents[1] / "scripts" / "config.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


def test_skill_file_precedence_empty_fallback_and_isolation(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("AIHUB_API_KEY=home-key\n")
    (tmp_path / ".env.unrelated-skill").write_text("AIHUB_API_KEY=wrong-skill\n")
    selected = tmp_path / ".env.memoji-sticker-pack"
    selected.write_text("AIHUB_API_KEY=skill-key\n")
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=local-key\n")
    (tmp_path / ".env").write_text("AIHUB_API_KEY=shared-key\n")
    def keys(env=None, use_home=False):
        return config.resolve_api_key_candidates(env or {}, str(tmp_path), use_home, str(home))
    assert [item.value for item in keys({"AIHUB_API_KEY": "process-key"})] == ["process-key", "skill-key", "local-key", "shared-key"]
    assert keys()[0].source == str(selected)
    selected.write_text("AIHUB_API_KEY=''\n")
    assert keys()[0].value == "local-key"
    (tmp_path / ".env.local").unlink()
    (tmp_path / ".env").unlink()
    assert keys() == []
    assert keys(use_home=True)[0].value == "home-key"
    child = tmp_path / "child"
    child.mkdir()
    selected.write_text("AIHUB_API_KEY=parent-key\n")
    assert config.resolve_api_key_candidates({}, str(child), False, str(home)) == []
