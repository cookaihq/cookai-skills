import io
import tarfile

import pytest

from frpc_launch import (resolve_official_release, parse_checksums,
                         extract_frpc_member, install_official, sha256_file,
                         ensure_home_layout, read_meta, FrpcLaunchError)

FRPC_BYTES = b"#!/bin/sh\necho 0.70.1\n"


def make_release(version="0.70.1", os_name="darwin", arch="arm64"):
    asset = "frp_%s_%s_%s.tar.gz" % (version, os_name, arch)
    return {
        "tag_name": "v" + version,
        "assets": [
            {"name": asset, "browser_download_url": "https://example.com/" + asset},
            {"name": "frp_sha256_checksums.txt",
             "browser_download_url": "https://example.com/frp_sha256_checksums.txt"},
        ],
    }


def make_tarball(tmp_path, version="0.70.1", os_name="darwin", arch="arm64"):
    root = "frp_%s_%s_%s" % (version, os_name, arch)
    tar_path = tmp_path / (root + ".tar.gz")
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(root + "/frpc")
        info.size = len(FRPC_BYTES)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(FRPC_BYTES))
    return tar_path


def test_resolve_release_exact_asset():
    version, name, url, csum_url = resolve_official_release(make_release(), "darwin", "arm64")
    assert version == "0.70.1"
    assert name == "frp_0.70.1_darwin_arm64.tar.gz"
    assert url.endswith(name) and csum_url.endswith("frp_sha256_checksums.txt")


def test_resolve_release_missing_asset_refuses():
    with pytest.raises(FrpcLaunchError):
        resolve_official_release(make_release(arch="riscv"), "darwin", "arm64")


def test_parse_checksums():
    text = "aaaa  frp_0.70.1_darwin_arm64.tar.gz\nbbbb  other.zip\n"
    assert parse_checksums(text)["frp_0.70.1_darwin_arm64.tar.gz"] == "aaaa"


def test_extract_only_expected_member(tmp_path):
    tar_path = make_tarball(tmp_path)
    dest = tmp_path / "frpc.tmp"
    extract_frpc_member(tar_path, "0.70.1", "darwin", "arm64", dest)
    assert dest.read_bytes() == FRPC_BYTES


def _fake_fetchers(tmp_path, tamper=False):
    tar_path = make_tarball(tmp_path)
    tar_bytes = tar_path.read_bytes()
    digest = sha256_file(tar_path)
    if tamper:
        digest = "0" * 64
    csum = "%s  frp_0.70.1_darwin_arm64.tar.gz\n" % digest

    def fetch_json(url, timeout=30):
        return make_release()

    def fetch_bytes(url, timeout=30):
        return csum.encode() if url.endswith(".txt") else tar_bytes

    return fetch_json, fetch_bytes


def test_install_official_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("frpc_launch.detect_platform", lambda: ("darwin", "macos", "arm64"))
    home = tmp_path / "home"
    ensure_home_layout(home)
    fj, fb = _fake_fetchers(tmp_path)
    meta = install_official(home, fetch_json=fj, fetch_bytes=fb)
    binary = home / "bin" / "macos" / "frpc"
    assert binary.read_bytes() == FRPC_BYTES
    saved = read_meta(home / "bin" / "macos" / "frpc.meta.json")
    assert saved["version"] == "0.70.1" == meta["version"]
    assert saved["sha256"] == sha256_file(binary)


def test_install_official_checksum_mismatch_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("frpc_launch.detect_platform", lambda: ("darwin", "macos", "arm64"))
    home = tmp_path / "home"
    ensure_home_layout(home)
    fj, fb = _fake_fetchers(tmp_path, tamper=True)
    with pytest.raises(FrpcLaunchError):
        install_official(home, fetch_json=fj, fetch_bytes=fb)
    assert not (home / "bin" / "macos" / "frpc").exists()
    assert not (home / "bin" / "macos" / "frpc.meta.json").exists()
