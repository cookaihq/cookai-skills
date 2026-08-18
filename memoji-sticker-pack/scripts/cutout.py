#!/usr/bin/env python3
"""把纯色（默认绿幕）背景的图抠成透明 PNG。

gpt-image-2 渠道不支持 background 透明参数，且对"transparent background"会画出
假棋盘格。对策：prompt 要求纯绿幕底，再用本脚本按"到角落色的距离"键控成透明，
并对保留像素做去绿边（despill）。主体是暖色系、无绿，所以不会误抠脸/衣服。

用法： uv run --project <skill目录> <skill目录>/scripts/cutout.py --in raw.png --out final.png [--hard 70] [--soft 150]
"""
import argparse
import os
import shlex
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。必须在
# import numpy / PIL **之前**——它们只装在该 venv 里。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_DIR = os.path.join(SKILL_DIR, os.environ.get("UV_PROJECT_ENVIRONMENT") or ".venv")


def _require(module_name, package_hint):
    """真实 import 预检（ADR 0007 §1.5）。

    用真实 import 而不是 find_spec：存在性探测探不出「包目录在、传递依赖缺失、
    一 import 就炸」的半残环境。捕获面必须宽于 ImportError——真实 import 会执行
    包的顶层代码，半残环境里那里可能抛 OSError / RuntimeError 等任意异常。
    失败时透出底层异常原文，并给可直接复制执行的重建命令。
    """
    try:
        import importlib

        return importlib.import_module(module_name)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：捕获面必须宽
        sys.stderr.write(
            "运行时依赖不可用：import %s 失败（%s 提供）。\n"
            "底层错误：%s: %s\n"
            "请按锁文件重建本 skill 的运行环境：%s\n"
            % (
                module_name,
                package_hint,
                type(exc).__name__,
                exc,
                # --no-dev：dev 依赖组（pytest 等）只服务本仓测试，不该进用户环境。
                "rm -rf %s && uv sync --project %s --no-dev"
                % (shlex.quote(VENV_DIR), shlex.quote(SKILL_DIR)),
            )
        )
        raise SystemExit(1)


np = _require("numpy", "numpy")
Image = _require("PIL.Image", "Pillow")


def corner_key(arr):
    """取四角小块的中位色作为背景键色（角落基本一定是背景）。"""
    h, w = arr.shape[:2]
    s = max(6, min(h, w) // 80)
    patches = [arr[0:s, 0:s], arr[0:s, w - s:w], arr[h - s:h, 0:s], arr[h - s:h, w - s:w]]
    cols = np.concatenate([p.reshape(-1, arr.shape[2]) for p in patches], axis=0)[:, :3]
    return np.median(cols.astype(np.float32), axis=0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--hard", type=float, default=70.0, help="距离<=hard 完全透明")
    ap.add_argument("--soft", type=float, default=150.0, help="距离>=soft 完全不透明")
    a = ap.parse_args()

    im = Image.open(a.inp).convert("RGBA")
    arr = np.array(im).astype(np.float32)
    rgb = arr[:, :, :3].copy()

    key = corner_key(arr)
    dist = np.sqrt(((rgb - key) ** 2).sum(axis=2))

    # 软边 alpha 斜坡
    denom = max(1e-3, (a.soft - a.hard))
    alpha = np.clip((dist - a.hard) / denom, 0.0, 1.0) * 255.0

    # 去绿边：键色偏绿时，把保留像素里"绿明显高于红蓝"的绿压回去
    if key[1] > key[0] and key[1] > key[2]:
        r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
        cap = np.maximum(r, b)
        rgb[:, :, 1] = np.where(g > cap, cap, g)

    out = np.dstack([rgb, alpha]).astype(np.uint8)
    Image.fromarray(out).save(a.out)

    transp = float((alpha < 10).mean() * 100.0)
    kept = float((alpha > 245).mean() * 100.0)
    print(f"[cutout] key={[int(x) for x in key.round()]} 透明={transp:.1f}% 保留={kept:.1f}%")


if __name__ == "__main__":
    main()
