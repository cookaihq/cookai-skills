"""Markdown 侧 Source Block Inventory（基线 §10.5）。

用 pandoc 3.10.1 `gfm+sourcepos -t json` 解析 Doc2X 直接稿，产出可重复的
block 列表：{block_id, reading_order, kind, span}；span 是 UTF-8 半开字节
区间（由 data-pos 的 line:col 换算）。kind 使用 §10.5 闭合词表。

实测 AST 形状（pandoc 3.10.1）：Header 的 attr 自带 data-pos；其余顶层块
由带 data-pos 的 Div 包裹；列表项没有独立 data-pos（v1 以整个列表为一个
list_item 组块记 composite/list_item）。
"""

import json
import subprocess

from . import jcs
from . import sourcefreeze

BLOCK_KINDS = sourcefreeze.BLOCK_KINDS

INVENTORY_SCHEMA_VERSION = "pdf2markdown.source-block-inventory/v1"


class MdInventoryError(ValueError):
    """解析失败；调用方映射 conversion_result_invalid。"""


def _pandoc_ast(markdown_bytes, pandoc_path):
    proc = subprocess.run(
        [pandoc_path, "-f", "gfm+sourcepos", "-t", "json"],
        input=markdown_bytes,
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise MdInventoryError("pandoc failed to parse markdown")
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        raise MdInventoryError("pandoc emitted invalid JSON AST")


def _line_offsets(markdown_bytes):
    """每行起始的 UTF-8 字节偏移（1-based line）。"""
    offsets = [0]
    for index, byte in enumerate(markdown_bytes):
        if byte == 0x0A:
            offsets.append(index + 1)
    return offsets


def _pos_to_offset(pos, line_offsets, markdown_bytes, line_lengths):
    """"line:col" → 字节偏移；col 是 1-based 字节列（pandoc sourcepos）。"""
    line_str, col_str = pos.split(":")
    line, col = int(line_str), int(col_str)
    if line == len(line_offsets) + 1:
        # 文件末尾无换行时，pandoc 区间末端指向最后一行的下一行开头
        # （one-past-end，live 定型）：钳制为文件末尾字节偏移。
        return len(markdown_bytes)
    if line < 1 or line > len(line_offsets):
        raise MdInventoryError("sourcepos line out of range")
    return min(line_offsets[line - 1] + (col - 1), len(markdown_bytes))


def _attr_pos(attr):
    for key, value in attr[2]:
        if key == "data-pos":
            return value
    return None


def _inline_types(inlines):
    out = set()
    for item in inlines:
        t = item.get("t")
        out.add(t)
        if t == "Span":
            out |= _inline_types(item["c"][1])
    return out


def _classify(block):
    t = block.get("t")
    if t == "Header":
        return "heading"
    if t == "Table":
        return "table"
    if t in ("BulletList", "OrderedList"):
        return "list_item"
    if t == "CodeBlock":
        return "composite"
    if t in ("Para", "Plain"):
        inline_types = _inline_types(block["c"])
        meaningful = inline_types - {"Space", "SoftBreak", "Span", "Str"}
        if "Image" in inline_types and meaningful <= {"Image"}:
            return "image"
        if "Math" in inline_types and meaningful <= {"Math"}:
            return "formula"
        if "Link" in inline_types and meaningful <= {"Link"}:
            return "link"
        return "paragraph"
    if t == "BlockQuote":
        return "composite"
    return "unknown"


def parse_blocks(markdown_bytes, pandoc_path):
    """返回按阅读顺序的 block 列表；block_id 可重复生成。"""
    ast = _pandoc_ast(markdown_bytes, pandoc_path)
    line_offsets = _line_offsets(markdown_bytes)
    blocks = []
    reading_order = 0
    for top in ast.get("blocks", []):
        inner = top
        pos = None
        if top.get("t") == "Div":
            pos = _attr_pos(top["c"][0])
            content = top["c"][1]
            inner = content[0] if len(content) == 1 else {"t": "Div", "c": top["c"]}
            if len(content) != 1:
                inner_kind = "composite"
            else:
                inner_kind = _classify(inner)
        elif top.get("t") == "Header":
            pos = _attr_pos(top["c"][1])
            inner_kind = "heading"
        else:
            inner_kind = _classify(top)
        span = None
        if pos:
            try:
                start_pos, end_pos = pos.split("-")
                start = _pos_to_offset(start_pos, line_offsets, markdown_bytes, None)
                end = _pos_to_offset(end_pos, line_offsets, markdown_bytes, None)
                if end < start:
                    raise MdInventoryError("sourcepos end before start")
                span = [start, end]
            except MdInventoryError:
                raise
            except Exception:
                span = None
        if inner_kind not in BLOCK_KINDS:
            inner_kind = "unknown"
        blocks.append(
            {
                "block_id": None,
                "reading_order": reading_order,
                "kind": inner_kind,
                "span": span,
            }
        )
        reading_order += 1
    _fill_missing_spans(blocks, len(markdown_bytes), markdown_bytes)
    for block in blocks:
        block["block_id"] = jcs.sha256_canonical(
            {
                "reading_order": block["reading_order"],
                "kind": block["kind"],
                "span": block["span"],
            }
        )[:32]
    return blocks


def _line_runs(markdown_bytes, start, end):
    """[start,end) 内的行列表：(line_start, line_end_incl_newline, stripped)。"""
    runs = []
    cursor = start
    while cursor < end:
        newline = markdown_bytes.find(b"\n", cursor, end)
        line_end = end if newline < 0 else newline + 1
        runs.append((cursor, line_end, markdown_bytes[cursor:line_end].strip()))
        cursor = line_end
    return runs


def _signature_span(kind, markdown_bytes, start, end):
    """在窗口内按内容签名定位块：table=连续 | 行；composite(code)=围栏对；
    其他=首个非空行到下个空行。找不到返回 None。"""
    lines = _line_runs(markdown_bytes, start, end)
    if kind == "table":
        run = [l for l in lines if l[2].startswith(b"|")]
        if not run:
            return None
        first = run[0]
        last = first
        for line in lines:
            if line[0] >= first[0]:
                if line[2].startswith(b"|"):
                    last = line
                elif line[0] > first[0] and line[2]:
                    break
        return [first[0], last[1]]
    if kind == "composite":
        fences = [l for l in lines if l[2].startswith(b"```")]
        if len(fences) >= 2:
            return [fences[0][0], fences[1][1]]
    content = [l for l in lines if l[2]]
    if not content:
        return None
    first = content[0]
    last = first
    for line in lines:
        if line[0] > first[0]:
            if line[2]:
                last = line
            else:
                break
    return [first[0], last[1]]


def _fill_missing_spans(blocks, total, markdown_bytes):
    """起点可靠、end 可能贪婪越界（实测 pandoc 3.10.1 sourcepos 行为）：
    1) 无 pos 块用内容签名在剩余窗口内定位；
    2) 全部起点确定后，把每块 end 钳制到下一块起点并裁尾部空白。"""
    cursor = 0
    for index, block in enumerate(blocks):
        if block["span"] is not None:
            # 已知起点：窗口游标推进到起点之后（end 不可靠，不用于游标）。
            cursor = max(cursor, block["span"][0] + 1)
            continue
        next_start = total
        for nxt in blocks[index + 1:]:
            if nxt["span"] is not None:
                next_start = nxt["span"][0]
                break
        located = _signature_span(block["kind"], markdown_bytes, cursor, next_start)
        if located is None:
            located = [cursor, next_start]
        block["span"] = located
        cursor = located[1]
    for index, block in enumerate(blocks):
        start, end = block["span"]
        if index + 1 < len(blocks):
            end = min(end, blocks[index + 1]["span"][0])
        else:
            end = min(end, total)
        segment = markdown_bytes[start:end]
        trailing = len(segment) - len(segment.rstrip(b"\n\r"))
        block["span"] = [start, max(start, end - trailing)]


def build_inventory(markdown_bytes, markdown_sha256, pandoc_path):
    """Source Block Inventory v1（Markdown 侧）；返回 (inventory, sha256)。"""
    blocks = parse_blocks(markdown_bytes, pandoc_path)
    inventory = {
        "schema_version": INVENTORY_SCHEMA_VERSION,
        "markdown_sha256": markdown_sha256,
        "block_count": len(blocks),
        "blocks": blocks,
    }
    return inventory, jcs.sha256_canonical(inventory)


def block_fragment(markdown_bytes, block):
    """按 span 取出该 block 的 Markdown 片段（review 单元输入）。"""
    span = block.get("span")
    if span is None:
        return ""
    return markdown_bytes[span[0]:span[1]].decode("utf-8", "replace")
