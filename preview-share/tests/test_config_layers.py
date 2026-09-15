import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("preview_share_layers", Path(__file__).resolve().parents[1] / "scripts" / "upload.py")
config = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(config)


def test_skill_file_isolated_per_variable_and_process_wins(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    names = ("PREVIEW_SHARE_FTP", "PREVIEW_SHARE_BASEURL")
    for name in names:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env.preview-share").write_text("PREVIEW_SHARE_FTP=skill-ftp\nPREVIEW_SHARE_BASEURL=''\n")
    (tmp_path / ".env.local").write_text("PREVIEW_SHARE_FTP=local-ftp\nPREVIEW_SHARE_BASEURL=local-url\n")
    (tmp_path / ".env.unrelated-skill").write_text("PREVIEW_SHARE_BASEURL=wrong-skill\n")
    def resolve():
        return config.resolve_config(False)
    values, sources = resolve()
    assert values[names[0]] == "skill-ftp"
    assert sources[names[0]] == "$PWD/.env.preview-share"
    assert values[names[1]] == "local-url"
    monkeypatch.setenv(names[0], "process-ftp")
    assert resolve()[0][names[0]] == "process-ftp"
    (tmp_path / ".env.local").unlink()
    assert names[1] not in resolve()[0]
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    monkeypatch.delenv(names[0])
    assert resolve()[0] == {}
