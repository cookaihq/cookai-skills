#!/usr/bin/env python3
"""为 Memoji 表情包生成 manifest.json + index.html 画廊。

由 gen_pack.sh 调用，也可单独运行：
  uv run --project <skill目录> <skill目录>/scripts/build_gallery.py \
      --outdir DIR --name NAME --base base.png [--items items.tsv]

items.tsv 每行： slug<TAB>label<TAB>filename
"""
import argparse
import json
import os
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()


def load_items(path):
    items = []
    if not path or not os.path.isfile(path):
        return items
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            slug, label, filename = parts[0], parts[1], parts[2]
            items.append({"slug": slug, "label": label, "file": filename})
    return items


def build_html(name, base, items):
    tiles = []
    if base:
        tiles.append(
            f'<figure class="tile"><div class="ph base"><img src="{base}" alt="基准"></div>'
            f'<figcaption>基准 Memoji<span class="t">base</span></figcaption></figure>'
        )
    for it in items:
        tiles.append(
            f'<figure class="tile"><div class="ph"><img src="{it["file"]}" alt="{it["label"]}"></div>'
            f'<figcaption>{it["label"]}<span class="t">{it["slug"]}</span></figcaption></figure>'
        )
    tiles_html = "\n".join(tiles)
    count = len(items)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{name} · Memoji 表情包</title>
<style>
:root{{color-scheme:light dark}}
*{{box-sizing:border-box}}
body{{font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro",system-ui,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}}
@media(prefers-color-scheme:dark){{body{{background:#000;color:#f5f5f7}}.tile{{background:#1c1c1e!important}}header{{background:#1c1c1e!important}}}}
header{{position:sticky;top:0;z-index:9;background:#fff;padding:18px 24px;box-shadow:0 1px 0 rgba(0,0,0,.08)}}
h1{{margin:0 0 4px;font-size:22px;font-weight:700}}
.sub{{color:#86868b;font-size:13px}}
main{{padding:18px 24px 60px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:14px}}
.tile{{margin:0;background:#fff;border-radius:16px;padding:10px;text-align:center;transition:transform .12s}}
.tile:hover{{transform:translateY(-3px)}}
.ph{{border-radius:12px;overflow:hidden;aspect-ratio:1;display:flex;align-items:center;justify-content:center;
  background-color:#fafafa;
  background-image:linear-gradient(45deg,#e9e9ec 25%,transparent 25%),linear-gradient(-45deg,#e9e9ec 25%,transparent 25%),linear-gradient(45deg,transparent 75%,#e9e9ec 75%),linear-gradient(-45deg,transparent 75%,#e9e9ec 75%);
  background-size:16px 16px;background-position:0 0,0 8px,8px -8px,-8px 0}}
.ph.base{{outline:2px solid #06c;outline-offset:-2px}}
.ph img{{width:100%;height:100%;object-fit:contain}}
figcaption{{margin-top:8px;font-size:12px;color:#515154}}
figcaption .t{{display:block;color:#86868b;font-size:10px;margin-top:1px}}
</style></head><body>
<header>
  <h1>{name} · Memoji 表情包</h1>
  <div class="sub">{count} 个表情 + 1 张基准头像 · 透明底 PNG（棋盘格用于显示透明区域）</div>
</header>
<main>
  <div class="grid">
{tiles_html}
  </div>
</main></body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--name", default="memoji")
    ap.add_argument("--base", default="")
    ap.add_argument("--items", default="")
    args = ap.parse_args()

    items = load_items(args.items)

    manifest = {
        "name": args.name,
        "base": args.base,
        "count": len(items),
        "stickers": items,
        "generated_with": "memoji-sticker-pack (image-2 / gpt-image-2)",
        "style": "Apple Memoji-style 3D avatar, transparent PNG",
    }
    with open(os.path.join(args.outdir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    html = build_html(args.name, args.base, items)
    with open(os.path.join(args.outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)

    print(f"[gallery] wrote manifest.json + index.html to {args.outdir} ({len(items)} stickers)")


if __name__ == "__main__":
    main()
