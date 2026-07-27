import copy
import hashlib
import json
from pathlib import Path

import pytest

from frpc_launch import (parse_sakura_clients, install_sakura, md5_file,
                         ensure_home_layout, read_meta, SakuraApiError)

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "sakura_clients.json").read_text())


def _set_url(payload, url):
    payload["frpc"]["archs"]["darwin_arm64"]["url"] = url


def _set_hash(payload, value):
    payload["frpc"]["archs"]["darwin_arm64"]["hash"] = value


def _fixture_with_body(body):
    payload = copy.deepcopy(FIXTURE)
    entry = payload["frpc"]["archs"]["darwin_arm64"]
    entry["size"] = len(body)
    entry["hash"] = hashlib.md5(body).hexdigest()
    return payload


def test_parse_real_fixture():
    info = parse_sakura_clients(FIXTURE, "darwin_arm64")
    assert info["url"].startswith("https://")
    assert isinstance(info["size"], int) and info["size"] > 0
    assert len(info["hash"]) == 32
    assert "-sakura-" in info["version"]


def _mutate(path_mutator):
    payload = copy.deepcopy(FIXTURE)
    path_mutator(payload)
    return payload


def test_reject_non_https_url():
    bad = _mutate(lambda p: _set_url(p, "http://nya.globalslb.net/x"))
    with pytest.raises(SakuraApiError):
        parse_sakura_clients(bad, "darwin_arm64")


def test_reject_unknown_host():
    bad = _mutate(lambda p: _set_url(p, "https://evil.example.com/frpc"))
    with pytest.raises(SakuraApiError):
        parse_sakura_clients(bad, "darwin_arm64")


def test_reject_unrecognized_hash_format():
    bad = _mutate(lambda p: _set_hash(p, "zz not a hash"))
    with pytest.raises(SakuraApiError):
        parse_sakura_clients(bad, "darwin_arm64")


def test_reject_missing_platform():
    with pytest.raises(SakuraApiError):
        parse_sakura_clients(FIXTURE, "plan9_386")


def test_install_sakura_verifies_size_and_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("frpc_launch.detect_platform", lambda: ("darwin", "macos", "arm64"))
    home = tmp_path / "home"
    ensure_home_layout(home)
    body = b"\x7fELF-fake-sakura-frpc"
    payload = _fixture_with_body(body)
    meta = install_sakura(home,
                          fetch_json=lambda u, timeout=30: payload,
                          fetch_bytes=lambda u, timeout=30: body)
    binary = home / "bin" / "macos" / "frpc-sakura"
    assert binary.read_bytes() == body
    saved = read_meta(home / "bin" / "macos" / "frpc-sakura.meta.json")
    assert saved["upstream_hash"] == md5_file(binary)
    assert saved["sha256"] and saved["version"] == meta["version"]


def test_install_sakura_size_mismatch_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("frpc_launch.detect_platform", lambda: ("darwin", "macos", "arm64"))
    home = tmp_path / "home"
    ensure_home_layout(home)
    payload = _fixture_with_body(b"\x7fELF-fake")
    with pytest.raises(SakuraApiError):
        install_sakura(home,
                       fetch_json=lambda u, timeout=30: payload,
                       fetch_bytes=lambda u, timeout=30: b"truncated")
    assert not (home / "bin" / "macos" / "frpc-sakura").exists()
