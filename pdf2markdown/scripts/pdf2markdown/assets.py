"""资源需求与最终稿改写（基线 §11.3、§10.1 工具链身份）。

- 结构化解析：pandoc（固定 3.10.1，可执行文件身份检查）把 Markdown 转
  HTML，再用 lxml 提取图片引用；不用正则扫源文本。
- 本地资源校验：containment、普通文件、大小、SHA-256；并集按 SHA 去重，
  角色固定 direct-only / processed-only / shared。
- Asset Requirement：producer=conversion-core、闭合版本、requirement hash。
"""

import hashlib
import json
import os
import re
import subprocess

from . import fsatomic

REQUIRED_PANDOC_VERSION = "3.10.1"

ASSET_MAX_COUNT = 65535
ASSET_MAX_TOTAL_BYTES = 268435456


class AssetError(ValueError):
    """资源发现/校验失败；调用方映射 asset_publication_invalid。"""


def find_pandoc(explicit_path=None):
    """定位并验证 pandoc 3.10.1（§10.1 可执行文件身份检查）。"""
    candidates = [explicit_path] if explicit_path else ["pandoc"]
    for candidate in candidates:
        try:
            proc = subprocess.run(
                [candidate, "--version"], capture_output=True, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        first = proc.stdout.decode("utf-8", "replace").splitlines()[:1]
        if proc.returncode == 0 and first and first[0].strip() == "pandoc %s" % REQUIRED_PANDOC_VERSION:
            return candidate
    raise AssetError("Pandoc %s required. Install the package for your OS from "
                     "https://github.com/jgm/pandoc/releases/tag/3.10.1 and pass --pandoc /path/to/pandoc."
                     % REQUIRED_PANDOC_VERSION)


def markdown_to_html(markdown_bytes, pandoc_path):
    proc = subprocess.run(
        [pandoc_path, "-f", "gfm", "-t", "html"],
        input=markdown_bytes,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise AssetError("pandoc failed to parse markdown")
    return proc.stdout


def discover_image_targets(markdown_bytes, pandoc_path):
    """结构化提取图片目标（文档序、保序去重）。"""
    from lxml import html as lxml_html

    raw = markdown_to_html(markdown_bytes, pandoc_path)
    if not raw.strip():
        return []
    tree = lxml_html.fromstring(raw)
    seen = []
    for img in tree.iter("img"):
        src = img.get("src")
        if src and src not in seen:
            seen.append(src)
    return seen


def validate_local_resources(targets, base_dir):
    """本地相对目标 → containment/普通文件/大小/SHA 校验；返回记录列表。"""
    base_real = os.path.realpath(base_dir)
    records = []
    total = 0
    if len(targets) > ASSET_MAX_COUNT:
        raise AssetError("asset count exceeds cap")
    for target in targets:
        if target.startswith(("http://", "https://", "data:", "blob:", "//")):
            raise AssetError("non-local asset target not allowed here")
        if target.startswith("/") or "\\" in target:
            raise AssetError("absolute path or backslash in asset target")
        joined = os.path.realpath(os.path.join(base_real, target))
        if joined != base_real and not joined.startswith(base_real + os.sep):
            raise AssetError("asset target escapes containment")
        st = fsatomic.check_safe_regular_file(joined, "asset")
        total += st.st_size
        if total > ASSET_MAX_TOTAL_BYTES:
            raise AssetError("asset total bytes exceed cap")
        records.append(
            {
                "target": target,
                "byte_count": st.st_size,
                "sha256": fsatomic.read_file_sha256(joined, "asset"),
            }
        )
    return records


def _plain_ast(markdown_bytes, pandoc_path):
    proc = subprocess.run([pandoc_path, "-f", "gfm", "-t", "json"], input=markdown_bytes,
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise AssetError("pandoc failed to verify image rewrite")
    return json.loads(proc.stdout)


def replace_image_targets(markdown_bytes, target_to_replacement, pandoc_path):
    """Patch image destinations, excluding code; verify all other AST content.

    A shared image/link reference or unsupported syntax is refused rather than
    silently rewriting ordinary content or losing a reference.
    """
    if not target_to_replacement:
        return markdown_bytes
    from . import mdinventory
    text = markdown_bytes.decode("utf-8")
    positioned = mdinventory._pandoc_ast(markdown_bytes, pandoc_path)
    offsets = mdinventory._line_offsets(markdown_bytes)
    protected = []

    def visit(value, callback):
        if isinstance(value, dict):
            callback(value)
            for child in value.values():
                visit(child, callback)
        elif isinstance(value, list):
            for child in value:
                visit(child, callback)

    def protect(node):
        if node.get("t") in ("Code", "CodeBlock"):
            pos = mdinventory._attr_pos(node["c"][0])
            if not pos:
                raise AssetError("code position unavailable during image rewrite")
            start, end = pos.split("-")
            bounds = [mdinventory._pos_to_offset(p, offsets, markdown_bytes, None) for p in (start, end)]
            protected.append(tuple(len(markdown_bytes[:bound].decode("utf-8")) for bound in bounds))
    visit(positioned, protect)
    patches = {}
    for target, replacement in target_to_replacement.items():
        escaped = re.escape(target)
        patterns = [
            r'(!\[[^\]\n]*\]\(\s*<?)(' + escaped + r')(?=>?(?:\s|\)))',
            r'(?m)^(\s{0,3}\[[^\]\n]+\]:\s*<?)(' + escaped + r')(?=>?(?:\s|$))',
            r'(<img\b[^>]*?\bsrc\s*=\s*["\x27])(' + escaped + r')(?=["\x27])',
        ]
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                start, end = match.span(2)
                if not any(start < high and end > low for low, high in protected):
                    patches[start, end] = replacement
    output = text
    for (start, end), replacement in sorted(patches.items(), reverse=True):
        output = output[:start] + replacement + output[end:]
    encoded = output.encode("utf-8")
    expected = _plain_ast(markdown_bytes, pandoc_path)

    def expected_change(node):
        if node.get("t") == "Image":
            target = node["c"][2][0]
            node["c"][2][0] = target_to_replacement.get(target, target)
        elif node.get("t") in ("RawInline", "RawBlock") and node["c"][0] == "html":
            raw = node["c"][1]
            for target, replacement in target_to_replacement.items():
                pattern = r'(<img\b[^>]*?\bsrc\s*=\s*["\x27])' + re.escape(target) + r'(?=["\x27])'
                raw = re.sub(pattern, lambda m: m.group(1) + replacement, raw)
            node["c"][1] = raw
    visit(expected, expected_change)
    if expected != _plain_ast(encoded, pandoc_path):
        raise AssetError("image rewrite would change non-image content or leave unsupported references")
    return encoded
