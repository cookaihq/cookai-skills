#!/usr/bin/env python3
"""template-preview: 把一组图片+文案渲染成「指定模板风格」的自包含展示页。

v2：输出个人主页 index.html + 每条笔记一个 note-NN.html（详情页，横向轮播）。
主页卡片链接到对应详情页；填充卡不可点、不生成详情页。

配置读取优先级（见仓库 CLAUDE.md 通用约定，本 skill 去掉 ~/.config 层）：
  1. 进程环境变量（本轮显式注入或已 export）
  2. $PWD/.env.local（自动读，不向上递归）
  3. $PWD/.env（自动读，不向上递归）
  4. 内置默认（模板级在 templates/<t>/defaults.env 与内置素材；skill 级写在本文件）
每个变量独立按此顺序取「首个非空来源」。
stdout 输出所有生成页面的绝对路径（每行一个，第一行为 index.html）；其余信息走 stderr。
"""
import argparse
import datetime
import html
import json
import os
import re
import shutil
import sys

# 运行时环境 bootstrap（ADR 0007 §1.4 脚本侧兜底）：直接执行本文件时，若解释器
# 不在 <skill>/.venv 内就 execv 拉回去（venv 缺失按 uv.lock 自动重建）。放在
# `__main__` 守卫里，好让 tests 的 exec_module 导入本模块时不触发 execv。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if __name__ == "__main__":
    import _runtime_bootstrap

    _runtime_bootstrap.ensure()

HERE = os.path.dirname(os.path.realpath(__file__))
TEMPLATES_DIR = os.path.join(os.path.dirname(HERE), "templates")

SKILL_DEFAULTS = {
    "TPL_OUTPUT_ROOT": "",  # 空 = 直接放在项目根 $PWD 下；填相对/绝对路径可换根
    "TPL_SUBDIR_PATTERN": "{date}-{time}-{label}",  # 仅 --name 未给时用于自动命名
}

# 各模板的人设变量前缀
TEMPLATE_VAR_PREFIX = {
    "xiaohongshu": "TPL_XHS_",
}

IMG_EXT = {".svg", ".jpg", ".jpeg", ".png", ".webp", ".gif"}

# 标题/正文取不到时的固定回落文案（真实笔记用；填充卡不受影响）
DEFAULT_TITLE = "暂无标题"
DEFAULT_BODY = "暂无正文"


def log(msg):
    print(msg, file=sys.stderr)


def parse_env_file(path):
    """极简 .env 解析（与 preview-share 一致）：KEY=value / "value" / 'value'，
    # 注释，空行；同名取最后一次；不做 shell 展开 / 命令替换 / 续行。"""
    vals = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                vals[k] = v
    except FileNotFoundError:
        pass
    return vals


def resolve_config(varnames, builtin_defaults):
    """返回 (cfg, src)。每个变量独立按 env > $PWD/.env.local > $PWD/.env > builtin 取首个非空。"""
    layers = [
        ("env", {k: os.environ[k] for k in varnames if os.environ.get(k)}),
        ("$PWD/.env.local", parse_env_file(os.path.join(os.getcwd(), ".env.local"))),
        ("$PWD/.env", parse_env_file(os.path.join(os.getcwd(), ".env"))),
        ("builtin", builtin_defaults),
    ]
    cfg, src = {}, {}
    for var in varnames:
        for label, d in layers:
            if d.get(var):
                cfg[var] = d[var]
                src[var] = label
                break
    return cfg, src


def slugify_label(label):
    s = re.sub(r"[^-A-Za-z0-9._一-鿿]+", "-", (label or "").strip()).strip("-")
    return s or "preview"


def render_subdir(pattern, label, now):
    return pattern.format(
        date=now.strftime("%Y%m%d"),
        time=now.strftime("%H%M%S"),
        label=slugify_label(label),
    ).strip("/")


def build_output_dir(out_arg, output_root, subdir, pwd):
    if out_arg:
        return os.path.abspath(out_arg)
    root = output_root if os.path.isabs(output_root) else os.path.join(pwd, output_root)
    return os.path.join(root, subdir)


def ext_of(path, default=".jpg"):
    e = os.path.splitext(path)[1].lower()
    return e if e else default


def placeholder_likes(seed):
    """确定性占位点赞数：同输入同输出，落在 100..9099。"""
    return sum(ord(c) for c in str(seed)) % 9000 + 100


def resolve_path(p, pwd):
    return p if os.path.isabs(p) else os.path.join(pwd, p)


def load_content(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def note_image_srcs(note, pwd):
    """该 note 的图片绝对路径列表：images 优先（按顺序），回落到 [cover]。"""
    imgs = note.get("images")
    if isinstance(imgs, list) and imgs:
        srcs = imgs
    elif note.get("cover"):
        srcs = [note["cover"]]
    else:
        srcs = []
    return [resolve_path(s, pwd) for s in srcs]


def load_fillers(filler_dir):
    """读填充卡目录：图片文件 + 同目录 titles.json（[{file,title,likes}]）。"""
    meta = {}
    titles_path = os.path.join(filler_dir, "titles.json")
    if os.path.isfile(titles_path):
        with open(titles_path, encoding="utf-8") as f:
            for item in json.load(f):
                meta[item.get("file")] = item
    fillers = []
    if not os.path.isdir(filler_dir):
        return fillers
    for fn in sorted(os.listdir(filler_dir)):
        if fn == "titles.json":
            continue
        if os.path.splitext(fn)[1].lower() not in IMG_EXT:
            continue
        m = meta.get(fn, {})
        fillers.append({
            "cover_path": os.path.join(filler_dir, fn),
            "title": m.get("title", ""),
            "likes": m.get("likes"),
        })
    return fillers


def plan_render(content, persona, fillers, pwd, min_cards):
    """装配 (home_cards, note_pages, copies)。

    home_cards: 主页卡片。真实卡含 href→详情页；填充卡无 href（不可点）。
    note_pages: 每条笔记一个详情页 {filename, title, body, likes, slides:[相对图]}。
    copies: [(src_abs, dest_basename_under_assets)]，含头像 + 各笔记图 + 填充图。
    所有卡作者头像/昵称统一用人设。"""
    avatar_rel = persona["avatar_rel"]
    copies = [(persona["avatar_path"], os.path.basename(avatar_rel))]
    home_cards = []
    note_pages = []

    for i, n in enumerate(content.get("notes", []), 1):
        srcs = note_image_srcs(n, pwd)
        title = (n.get("title") or "").strip() or DEFAULT_TITLE   # 取不到→固定回落
        body = (n.get("body") or "").strip() or DEFAULT_BODY      # 取不到→固定回落
        likes = n.get("likes")
        if likes is None:
            likes = placeholder_likes(title)
        slides = []
        for m, src in enumerate(srcs, 1):
            dest = f"note-{i:02d}-img-{m:02d}{ext_of(src)}"
            copies.append((src, dest))
            slides.append(f"assets/{dest}")
        page_file = f"note-{i:02d}.html"
        home_cards.append({                       # 主页真实卡：封面=首图，点击进详情页
            "cover": slides[0] if slides else "",
            "title": title, "likes": likes,
            "author": persona["nickname"], "avatar": avatar_rel,
            "href": page_file,
        })
        note_pages.append({
            "filename": page_file, "title": title, "body": body,
            "likes": likes, "slides": slides,
        })

    for j, f in enumerate(fillers, 1):            # 填充卡补足网格：无 href、不出详情页
        if len(home_cards) >= min_cards:
            break
        src = f["cover_path"]
        dest = f"filler-{j:02d}{ext_of(src, '.svg')}"
        copies.append((src, dest))
        likes = f.get("likes")
        if likes is None:
            likes = placeholder_likes(f.get("title", ""))
        home_cards.append({
            "cover": f"assets/{dest}", "title": f.get("title", ""), "likes": likes,
            "author": persona["nickname"], "avatar": avatar_rel,
        })

    return home_cards, note_pages, copies


def format_count(n):
    """小红书风格计数：>=10000 显示「x.x万」（去掉 .0），否则原样。"""
    try:
        v = int(str(n).strip())
    except (ValueError, TypeError):
        return html.escape(str(n))
    if v < 10000:
        return str(v)
    s = f"{v / 10000:.1f}".rstrip("0").rstrip(".")
    return f"{s}万"


def render_card_html(card):
    inner = (
        f'<img class="cover" src="{card["cover"]}" alt="" loading="lazy">'
        '<div class="card-body">'
        f'<div class="card-title">{html.escape(card["title"])}</div>'
        '<div class="card-foot">'
        f'<span class="author"><img class="avatar-sm" src="{card["avatar"]}" alt="">'
        f'<span class="author-name">{html.escape(card["author"])}</span></span>'
        f'<span class="likes">♥ {format_count(card["likes"])}</span>'
        '</div></div>'
    )
    href = card.get("href")
    if href:  # 真实卡 → 整块可点链接到详情页
        return f'<a class="card-link" href="{html.escape(href)}"><div class="card">{inner}</div></a>'
    return f'<div class="card">{inner}</div>'  # 填充卡 → 不可点


def render_slides_html(slide_rels):
    return "\n".join(
        f'<div class="slide"><img src="{r}" alt="" loading="lazy"></div>' for r in slide_rels
    )


def render_dots_html(n):
    if n < 2:  # 单图不显示圆点
        return ""
    return "".join(
        f'<span class="dot{" active" if k == 0 else ""}"></span>' for k in range(n)
    )


def render_body_html(body):
    """正文：HTML 转义 + 空行切段（\\n\\n→<p>，段内 \\n→<br>）；空则整块不渲染。"""
    body = (body or "").strip()
    if not body:
        return ""
    paras = []
    for chunk in re.split(r"\n\s*\n", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        paras.append("<p>" + html.escape(chunk).replace("\n", "<br>") + "</p>")
    if not paras:
        return ""
    return '<div class="body">' + "\n".join(paras) + "</div>"


_PERSONA_TOKEN_RE = re.compile(
    r"\{\{(NICKNAME|BIO|RED_ID|FOLLOWING|FOLLOWERS|LIKES|AVATAR)\}\}")


def _persona_values(persona):
    return {
        "NICKNAME": html.escape(persona["nickname"]),
        "BIO": html.escape(persona["bio"]),
        "RED_ID": html.escape(persona["red_id"]),
        "FOLLOWING": format_count(persona["following"]),
        "FOLLOWERS": format_count(persona["followers"]),
        "LIKES": format_count(persona["likes"]),
        "AVATAR": persona["avatar_rel"],  # 相对路径，不转义
    }


def _sub_persona(template_str, persona):
    """单遍替换人设 token（插入值不会被再次扫描成 token）。"""
    values = _persona_values(persona)
    return _PERSONA_TOKEN_RE.sub(lambda m: values[m.group(1)], template_str)


def render_home_html(template_str, persona, home_cards):
    out = _sub_persona(template_str, persona)
    cards_html = "\n".join(render_card_html(c) for c in home_cards)
    return out.replace("<!--CARDS-->", cards_html)


def render_note_html(template_str, persona, page):
    out = _sub_persona(template_str, persona)
    out = out.replace("<!--SLIDES-->", render_slides_html(page["slides"]))
    out = out.replace("<!--DOTS-->", render_dots_html(len(page["slides"])))
    out = out.replace("<!--BODY-->", render_body_html(page["body"]))
    out = out.replace("{{LIKES_NOTE}}", format_count(page["likes"]))
    out = out.replace("{{TITLE}}", html.escape(page["title"]))
    return out


def _build_persona(prefix, persona_cfg, template_dir, pwd):
    raw_avatar = persona_cfg.get(prefix + "AVATAR")
    if raw_avatar:
        avatar_path = raw_avatar if os.path.isabs(raw_avatar) else os.path.join(pwd, raw_avatar)
    else:
        avatar_path = os.path.join(template_dir, "assets", "avatar-default.svg")
    return {
        "nickname": persona_cfg.get(prefix + "NICKNAME", ""),
        "bio": persona_cfg.get(prefix + "BIO", ""),
        "red_id": persona_cfg.get(prefix + "RED_ID", ""),
        "following": persona_cfg.get(prefix + "FOLLOWING", "0"),
        "followers": persona_cfg.get(prefix + "FOLLOWERS", "0"),
        "likes": persona_cfg.get(prefix + "LIKES", "0"),
        "avatar_path": avatar_path,
        "avatar_rel": "assets/avatar" + ext_of(avatar_path, ".svg"),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="把图片+文案渲染成指定模板风格的自包含展示页")
    ap.add_argument("--template", required=True, help="模板名（v1 仅 xiaohongshu）")
    ap.add_argument("--content", required=True, help="content.json 路径")
    ap.add_argument("--label", default="", help="子目录标签，--name 未给时参与 TPL_SUBDIR_PATTERN 自动命名")
    ap.add_argument("--name", default="", help="输出文件夹名（leaf）；给定则用它命名，跳过 date-time 自动命名")
    ap.add_argument("--out-root", dest="out_root", default="",
                    help="输出父目录（相对则相对 $PWD）；优先级高于 TPL_OUTPUT_ROOT，默认 $PWD")
    ap.add_argument("--out", help="直接指定完整输出目录（最高优先级，覆盖 root/name/pattern）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写盘")
    args = ap.parse_args(argv)

    pwd = os.getcwd()
    now = datetime.datetime.now()

    template_dir = os.path.join(TEMPLATES_DIR, args.template)
    prefix = TEMPLATE_VAR_PREFIX.get(args.template)
    if not os.path.isdir(template_dir) or prefix is None:
        log(f"[error] 未知模板: {args.template}（可用: {', '.join(TEMPLATE_VAR_PREFIX)}）")
        return 2
    if not os.path.isfile(args.content):
        log(f"[error] content.json 不存在: {args.content}")
        return 2

    skill_cfg, _ = resolve_config(list(SKILL_DEFAULTS), SKILL_DEFAULTS)
    tmpl_defaults = parse_env_file(os.path.join(template_dir, "defaults.env"))
    suffixes = ("NICKNAME", "BIO", "RED_ID", "AVATAR", "FOLLOWING",
                "FOLLOWERS", "LIKES", "FILLER_CARDS", "MIN_CARDS")
    persona_cfg, _ = resolve_config([prefix + s for s in suffixes], tmpl_defaults)

    persona = _build_persona(prefix, persona_cfg, template_dir, pwd)
    try:
        min_cards = int(persona_cfg.get(prefix + "MIN_CARDS") or 6)
    except ValueError:
        log(f"[error] {prefix}MIN_CARDS 不是整数: {persona_cfg.get(prefix + 'MIN_CARDS')!r}")
        return 2

    raw_fillers = persona_cfg.get(prefix + "FILLER_CARDS")
    if raw_fillers:
        filler_dir = raw_fillers if os.path.isabs(raw_fillers) else os.path.join(pwd, raw_fillers)
    else:
        filler_dir = os.path.join(template_dir, "assets", "fillers")

    try:
        content = load_content(args.content)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        log(f"[error] content.json 解析失败: {e}")
        return 2
    notes = content.get("notes", [])
    if not isinstance(notes, list):
        log("[error] content.json 的 notes 必须是数组")
        return 2
    for i, n in enumerate(notes, 1):
        if not isinstance(n, dict):
            log(f"[error] notes[{i}] 必须是对象")
            return 2
        imgs = n.get("images")
        if not (isinstance(imgs, list) and imgs) and not n.get("cover"):
            log(f"[error] notes[{i}] 需要 images（非空数组）或 cover 字段")
            return 2
    fillers = load_fillers(filler_dir)
    home_cards, note_pages, copies = plan_render(content, persona, fillers, pwd, min_cards)

    label = args.label or content.get("label", "")
    if args.name:
        subdir = slugify_label(args.name)        # 用户指定文件夹名，跳过自动命名
    else:
        try:
            subdir = render_subdir(skill_cfg["TPL_SUBDIR_PATTERN"], label, now)
        except KeyError as e:
            log(f"[error] TPL_SUBDIR_PATTERN 含未知占位符: {e}；仅支持 {{date}}/{{time}}/{{label}}")
            return 2
    out_root = args.out_root or skill_cfg.get("TPL_OUTPUT_ROOT", "")  # --out-root > env/.env > 默认($PWD)
    out_dir = build_output_dir(args.out, out_root, subdir, pwd)
    index_path = os.path.join(out_dir, "index.html")
    page_paths = [index_path] + [os.path.join(out_dir, p["filename"]) for p in note_pages]

    log(f"[plan] 模板: {args.template}")
    log(f"[plan] 输出目录: {out_dir}")
    log(f"[plan] 页面: 1 主页 + {len(note_pages)} 笔记页")
    log(f"[plan] 主页卡片: {len(home_cards)}（笔记 {len(notes)} + 填充，min={min_cards}）")
    for p in note_pages:
        log(f"   {p['filename']}  ({len(p['slides'])} 图)  {p['title']}")
    for src, dest in copies:
        warn = "" if os.path.isfile(src) else "  [warn 源缺失，线上会裂图]"
        log(f"   assets/{dest} <- {src}{warn}")

    if args.dry_run:
        log("[dry-run] 未写盘。")
        for p in page_paths:
            print(p)
        return 0

    if not args.out and os.path.exists(out_dir):
        log(f"[error] 输出目录已存在，拒绝覆盖: {out_dir}")
        log("  换 --name，或（自动命名时）在 TPL_SUBDIR_PATTERN 中保留 {time} 以保证唯一。")
        return 2

    with open(os.path.join(template_dir, "home.html"), encoding="utf-8") as tf:
        home_tmpl = tf.read()
    with open(os.path.join(template_dir, "note.html"), encoding="utf-8") as tf:
        note_tmpl = tf.read()

    assets_dir = os.path.join(out_dir, "assets")
    os.makedirs(assets_dir, exist_ok=True)
    for src, dest in copies:
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(assets_dir, dest))
        else:
            log(f"[warn] 跳过缺失素材: {src}")

    with open(index_path, "w", encoding="utf-8") as f:
        f.write(render_home_html(home_tmpl, persona, home_cards))
    for p in note_pages:
        with open(os.path.join(out_dir, p["filename"]), "w", encoding="utf-8") as f:
            f.write(render_note_html(note_tmpl, persona, p))
    with open(os.path.join(out_dir, "content.json"), "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)

    log(f"[done] 输出: {out_dir}（{len(page_paths)} 个页面）")
    for p in page_paths:
        print(p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
