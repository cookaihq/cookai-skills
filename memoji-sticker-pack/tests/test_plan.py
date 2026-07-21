import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_pack.sh"


def run_plan(tmp_path, *args):
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    return subprocess.run(
        ["bash", str(SCRIPT), *args, "--plan"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_pack_plan_counts_generation_retries_and_uploads(tmp_path):
    result = run_plan(tmp_path, "--image", "person.jpg", "--count", "2")

    assert result.returncode == 0
    assert "基准 Memoji 生成调用: 1 次" in result.stdout
    assert "image-2 生成调用总数（无重试）: 3 次" in result.stdout
    assert "最大生成调用数（每次失败生成最多重试 1 次）: 6 次" in result.stdout
    assert "文件上传调用: 2 次" in result.stdout
    assert "内置上传脚本:" in result.stdout


def test_no_retry_plan_omits_retry_maximum(tmp_path):
    result = run_plan(
        tmp_path,
        "--image",
        "person.jpg",
        "--count",
        "2",
        "--no-retry",
    )

    assert result.returncode == 0
    assert "image-2 生成调用总数（无重试）: 3 次" in result.stdout
    assert "最大生成调用数" not in result.stdout


def test_reused_base_pack_skips_base_generation_and_input_upload(tmp_path):
    result = run_plan(
        tmp_path,
        "--base-url",
        "https://example.com/base.png",
        "--count",
        "2",
    )

    assert result.returncode == 0
    assert "基准 Memoji 生成调用: 0 次" in result.stdout
    assert "image-2 生成调用总数（无重试）: 2 次" in result.stdout
    assert "最大生成调用数（每次失败生成最多重试 1 次）: 4 次" in result.stdout
    assert "文件上传调用: 1 次" in result.stdout


def test_reused_base_single_needs_no_generation_or_upload_dependency(tmp_path):
    result = run_plan(
        tmp_path,
        "--base-url",
        "https://example.com/base.png",
        "--mode",
        "single",
    )

    assert result.returncode == 0
    assert "image-2 生成调用总数（无重试）: 0 次" in result.stdout
    assert "文件上传调用: 0 次" in result.stdout
    assert "image-2: 本次不需要" in result.stdout
    assert "内置上传: 本次不需要" in result.stdout


def test_reused_base_single_runs_without_sibling_skills(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(
        """#!/bin/sh
out=''
while [ "$#" -gt 0 ]; do
  if [ "$1" = '-o' ]; then out="$2"; shift 2; else shift; fi
done
printf 'fake-image' > "$out"
""",
        encoding="utf-8",
    )
    python3 = bin_dir / "python3"
    python3.write_text(
        """#!/bin/sh
case "$1" in
  *cutout.py)
    shift
    src=''; out=''
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --in) src="$2"; shift 2 ;;
        --out) out="$2"; shift 2 ;;
        *) shift ;;
      esac
    done
    cp "$src" "$out"
    ;;
esac
""",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    python3.chmod(0o755)

    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    outdir = tmp_path / "result"
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--base-url",
            "https://example.com/base.png",
            "--mode",
            "single",
            "--outdir",
            str(outdir),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (outdir / "base.png").read_bytes() == b"fake-image"
