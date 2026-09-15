from pathlib import Path
import importlib.util
import sys


SPEC = importlib.util.spec_from_file_location("image_2_config_layers", Path(__file__).resolve().parents[2] / "scripts" / "image_task.py")
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


def test_skill_specific_layer_and_scalar_options(tmp_path):
    dedicated = tmp_path / ".env.image-2"
    dedicated.write_text("AIHUB_API_KEY=skill-key\nIMAGE_2_OUTPUT_DIR=skill-output\nAIHUBMAX_BASE_URL=https://skill.example\n")
    (tmp_path / ".env.local").write_text("AIHUB_API_KEY=local-key\nIMAGE_2_OUTPUT_DIR=local-output\n")
    (tmp_path / ".env.other-skill").write_text("AIHUB_API_KEY=other-key\n")
    home = tmp_path / "home"
    keys = runtime.collect_keys({}, tmp_path, home, False)
    assert [item.value for item in keys] == ["skill-key", "local-key"]
    assert keys[0].source == str(dedicated)
    assert runtime.collect_keys({"AIHUB_API_KEY": "process-key"}, tmp_path, home, False)[0].value == "process-key"
    assert runtime.resolve_variable("IMAGE_2_OUTPUT_DIR", {}, tmp_path, home, False) == "skill-output"
    assert runtime.resolve_variable("AIHUBMAX_BASE_URL", {}, tmp_path, home, False) == "https://skill.example"
    dedicated.write_text("AIHUB_API_KEY=''\nIMAGE_2_OUTPUT_DIR=''\n")
    assert runtime.collect_keys({}, tmp_path, home, False)[0].value == "local-key"
    assert runtime.resolve_variable("IMAGE_2_OUTPUT_DIR", {}, tmp_path, home, False) == "local-output"
    (tmp_path / ".env.local").unlink()
    assert runtime.collect_keys({}, tmp_path, home, False) == []
    child = tmp_path / "child"
    child.mkdir()
    dedicated.write_text("AIHUB_API_KEY=parent-key\n")
    assert runtime.collect_keys({}, child, home, False) == []
