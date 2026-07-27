import hashlib
import os
import stat

from frpc_launch import (detect_platform, ensure_home_layout, write_meta,
                         read_meta, install_binary, sha256_file, md5_file)


def test_detect_platform_current_host():
    os_name, os_subdir, arch = detect_platform()   # 开发机为 macOS 或 Linux
    assert (os_name, os_subdir) in [("darwin", "macos"), ("linux", "linux")]
    assert arch in ("amd64", "arm64")


def test_ensure_home_layout(tmp_path):
    home = tmp_path / "h"
    ensure_home_layout(home)
    for sub in ["bin/macos", "bin/linux", "bin/windows", "run"]:
        assert (home / sub).is_dir()
    assert stat.S_IMODE(home.stat().st_mode) == 0o700


def test_meta_roundtrip_and_bad_json(tmp_path):
    p = tmp_path / "frpc.meta.json"
    meta = {"version": "0.70.1", "source_url": "https://x", "sha256": "ab", "installed_at": "t"}
    write_meta(p, meta)
    assert read_meta(p) == meta
    p.write_text("{broken", encoding="utf-8")
    assert read_meta(p) == {}
    assert read_meta(tmp_path / "missing.json") == {}


def test_install_binary_atomic_and_exec(tmp_path):
    tmp = tmp_path / "dl.tmp"
    tmp.write_bytes(b"#!/bin/sh\necho ok\n")
    final = tmp_path / "frpc"
    install_binary(tmp, final)
    assert not tmp.exists() and final.exists()
    assert os.access(final, os.X_OK)


def test_hash_helpers(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"hello")
    assert sha256_file(f) == hashlib.sha256(b"hello").hexdigest()
    assert md5_file(f) == hashlib.md5(b"hello").hexdigest()
