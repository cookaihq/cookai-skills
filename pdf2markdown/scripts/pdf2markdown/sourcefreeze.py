"""源冻结与页基准（基线 §10.2）。

- 一次有界流式复制冻结副本，同时计算完整 SHA-256 与字节数；恢复再次核验。
- 页图合同 `page-reference-render/v1`：PyMuPDF==1.28.0，应用 page rotation
  后以 144 DPI、RGB、无 alpha、完整 MediaBox 逐页输出 PNG；宽/高 ≤8192 px、
  单页 ≤40,000,000 px、单 PNG ≤67,108,864 bytes、累计 ≤4,294,967,296 bytes；
  串行渲染且内存中最多保留一个完整 pixmap。
- Doc2X POST 前从冻结 PDF 与页基准图生成 `source-structure-inventory/v1`。
"""

import hashlib
import os

from . import fsatomic, jcs

PAGE_RENDER_POLICY = "page-reference-render/v1"
INVENTORY_SCHEMA_VERSION = "pdf2markdown.source-structure-inventory/v1"

SOURCE_PDF_MAX_BYTES = 268435456
SOURCE_PDF_MAX_PAGES = 2000
RENDER_DPI = 144
PAGE_MAX_EDGE_PX = 8192
PAGE_MAX_PIXELS = 40000000
PAGE_PNG_MAX_BYTES = 67108864
PAGES_TOTAL_MAX_BYTES = 4294967296

REQUIRED_PYMUPDF_VERSION = "1.28.0"

# 中立 kind 闭合集合（§10.5 Source Block Inventory v1 与 §10.2 结构预检共用词表）。
BLOCK_KINDS = (
    "heading",
    "paragraph",
    "list_item",
    "table",
    "formula",
    "image",
    "caption",
    "footnote",
    "link",
    "composite",
    "unknown",
)


class SourceFreezeError(ValueError):
    """源冻结/页基准失败；调用方映射为 conversion-core/source_invalid。"""


def _import_fitz():
    import fitz  # PyMuPDF

    version = getattr(fitz, "__version__", None) or fitz.VersionBind
    if version != REQUIRED_PYMUPDF_VERSION:
        raise SourceFreezeError(
            "PyMuPDF version %r does not match locked %s"
            % (version, REQUIRED_PYMUPDF_VERSION)
        )
    return fitz


def freeze_source(input_path, dest_path):
    """有界流式复制冻结副本；返回 (sha256, byte_count)。

    输入必须是受大小限制、合法 signature、非链接普通文件（§10.2）。
    """
    fsatomic.check_safe_regular_file(input_path, "source pdf")
    digest = hashlib.sha256()
    byte_count = 0
    head = b""
    directory = os.path.dirname(dest_path)
    fsatomic.ensure_private_dir(directory, "freeze dir")
    tmp_path = dest_path + ".tmp"
    fd = fsatomic.open_regular(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, "source copy")
    try:
        with os.fdopen(fsatomic.open_regular(input_path, os.O_RDONLY, "source pdf"), "rb") as src:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                if byte_count == 0:
                    head = chunk[:5]
                byte_count += len(chunk)
                if byte_count > SOURCE_PDF_MAX_BYTES:
                    raise SourceFreezeError("source pdf exceeds byte cap")
                digest.update(chunk)
                fsatomic.write_all(fd, chunk)
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    if head != b"%PDF-":
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise SourceFreezeError("input lacks PDF signature")
    os.rename(tmp_path, dest_path)
    fsatomic.fsync_dir(directory)
    readback = fsatomic.read_file_sha256(dest_path, "frozen source")
    if readback != digest.hexdigest():
        raise SourceFreezeError("frozen copy readback mismatch")
    return digest.hexdigest(), byte_count


def verify_frozen_source(dest_path, expected_sha256, expected_bytes):
    """恢复时再次核验冻结副本身份（§10.2）。"""
    st = fsatomic.check_safe_regular_file(dest_path, "frozen source")
    if st.st_size != expected_bytes:
        raise SourceFreezeError("frozen copy byte count drifted")
    if fsatomic.read_file_sha256(dest_path, "frozen source") != expected_sha256:
        raise SourceFreezeError("frozen copy sha drifted")


def render_page_references(frozen_pdf_path, out_dir):
    """按 page-reference-render/v1 逐页渲染 PNG；返回页记录列表。

    每条记录：{page_number, path, width_px, height_px, byte_count, sha256,
    rotation, mediabox}；任何上限失败抛 SourceFreezeError（Doc2X POST 前
    返回 source_invalid）。
    """
    fitz = _import_fitz()
    fsatomic.ensure_private_dir(out_dir, "page render dir")
    try:
        doc = fitz.open(frozen_pdf_path)
    except Exception as exc:
        raise SourceFreezeError("source PDF cannot be parsed (%s)" % type(exc).__name__) from None
    try:
        if doc.needs_pass:
            raise SourceFreezeError("encrypted pdf not supported")
        page_count = doc.page_count
        if page_count == 0:
            raise SourceFreezeError("pdf has no pages")
        if page_count > SOURCE_PDF_MAX_PAGES:
            raise SourceFreezeError("pdf exceeds page cap")
        zoom = RENDER_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        records = []
        total_bytes = 0
        for index in range(page_count):
            page = doc.load_page(index)
            rect = page.rect  # 应用 rotation 后的完整 MediaBox 视图
            width_px = int(rect.width * zoom + 0.5)
            height_px = int(rect.height * zoom + 0.5)
            if width_px <= 0 or height_px <= 0:
                raise SourceFreezeError("page %d has empty mediabox" % (index + 1))
            if width_px > PAGE_MAX_EDGE_PX or height_px > PAGE_MAX_EDGE_PX:
                raise SourceFreezeError("page %d exceeds edge cap" % (index + 1))
            if width_px * height_px > PAGE_MAX_PIXELS:
                raise SourceFreezeError("page %d exceeds pixel cap" % (index + 1))
            pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
            try:
                png_bytes = pixmap.tobytes("png")
            finally:
                pixmap = None  # 内存中最多保留一个完整 pixmap
            if len(png_bytes) > PAGE_PNG_MAX_BYTES:
                raise SourceFreezeError("page %d png exceeds byte cap" % (index + 1))
            total_bytes += len(png_bytes)
            if total_bytes > PAGES_TOTAL_MAX_BYTES:
                raise SourceFreezeError("page renders exceed total byte cap")
            path = os.path.join(out_dir, "page-%04d.png" % (index + 1))
            fsatomic.atomic_write_bytes(path, png_bytes, "page render")
            records.append(
                {
                    "page_number": index + 1,
                    "path": path,
                    "width_px": width_px,
                    "height_px": height_px,
                    "byte_count": len(png_bytes),
                    "sha256": hashlib.sha256(png_bytes).hexdigest(),
                    "rotation": page.rotation,
                    "mediabox": [rect.x0, rect.y0, rect.x1, rect.y1],
                    "render_policy": PAGE_RENDER_POLICY,
                    "dpi": RENDER_DPI,
                }
            )
        return records
    finally:
        doc.close()


def _normalize_bbox(rect, page_rect):
    """左上原点、[0,1] 范围、六位小数规范化（§11.1）。"""
    width = page_rect.width or 1.0
    height = page_rect.height or 1.0

    def clamp(v):
        return max(0.0, min(1.0, v))

    x0 = clamp((rect[0] - page_rect.x0) / width)
    y0 = clamp((rect[1] - page_rect.y0) / height)
    x1 = clamp((rect[2] - page_rect.x0) / width)
    y1 = clamp((rect[3] - page_rect.y0) / height)
    return [round(x0, 6), round(y0, 6), round(x1, 6), round(y1, 6)]


def build_source_structure_inventory(frozen_pdf_path, source_sha256, page_records):
    """生成严格 `source-structure-inventory/v1`（§10.2）。

    按页记录可重复 structure ID、阅读顺序、中立 kind、归一化 bbox；
    表格通过 PyMuPDF find_tables 检测（kind=table），文本块 kind=paragraph，
    图片块 kind=image；不做品牌判断。返回 (inventory_obj, sha256)。
    """
    fitz = _import_fitz()
    try:
        doc = fitz.open(frozen_pdf_path)
    except Exception as exc:
        raise SourceFreezeError("source PDF cannot be parsed (%s)" % type(exc).__name__) from None
    try:
        if doc.page_count != len(page_records):
            raise SourceFreezeError("page record count mismatch")
        pages = []
        for index in range(doc.page_count):
            page = doc.load_page(index)
            page_rect = page.rect
            blocks = []
            reading_order = 0
            table_rects = []
            try:
                tables = page.find_tables()
                table_rects = [tuple(t.bbox) for t in tables.tables]
            except Exception:
                table_rects = []
            for rect in table_rects:
                bbox = _normalize_bbox(rect, page_rect)
                blocks.append(("table", bbox))
            raw = page.get_text("dict")
            for block in raw.get("blocks", []):
                bbox_rect = block.get("bbox", (0, 0, 0, 0))
                # 落入已识别表格区域的文本块并入表格，不重复登记。
                inside_table = any(
                    bbox_rect[0] >= tr[0] - 1
                    and bbox_rect[1] >= tr[1] - 1
                    and bbox_rect[2] <= tr[2] + 1
                    and bbox_rect[3] <= tr[3] + 1
                    for tr in table_rects
                )
                if inside_table:
                    continue
                kind = "image" if block.get("type") == 1 else "paragraph"
                blocks.append((kind, _normalize_bbox(bbox_rect, page_rect)))
            # 阅读顺序：按 (y0, x0) 稳定排序。
            blocks.sort(key=lambda item: (item[1][1], item[1][0]))
            entries = []
            for kind, bbox in blocks:
                if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    kind = "unknown"  # 负面积/退化 bbox：保留为 unknown（§11.1）
                structure_id = jcs.sha256_canonical(
                    {
                        "source_sha256": source_sha256,
                        "page_number": index + 1,
                        "reading_order": reading_order,
                        "kind": kind,
                        "bbox": bbox,
                    }
                )[:32]
                entries.append(
                    {
                        "structure_id": structure_id,
                        "reading_order": reading_order,
                        "kind": kind,
                        "bbox": bbox,
                    }
                )
                reading_order += 1
            pages.append(
                {
                    "page_number": index + 1,
                    "page_sha256": page_records[index]["sha256"],
                    "blocks": entries,
                }
            )
        inventory = {
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "source_sha256": source_sha256,
            "page_count": doc.page_count,
            "render_policy": PAGE_RENDER_POLICY,
            "pages": pages,
        }
        return inventory, jcs.sha256_canonical(inventory)
    finally:
        doc.close()


def cross_page_table_precheck(inventory):
    """`cross_page_table_geometry/v1`（§10.3）：相邻页 前页最后 table y1>=0.98
    且后页第一个 table y0<=0.02 时启用跨页表合并。返回 (enabled, evidence)。
    """
    threshold_version = "cross_page_table_geometry/v1"
    matched_pairs = []
    pages = inventory["pages"]
    for i in range(len(pages) - 1):
        prev_tables = [b for b in pages[i]["blocks"] if b["kind"] == "table"]
        next_tables = [b for b in pages[i + 1]["blocks"] if b["kind"] == "table"]
        if not prev_tables or not next_tables:
            continue
        prev_last = prev_tables[-1]
        next_first = next_tables[0]
        if prev_last["bbox"][3] >= 0.98 and next_first["bbox"][1] <= 0.02:
            matched_pairs.append(
                {
                    "prev_page": pages[i]["page_number"],
                    "next_page": pages[i + 1]["page_number"],
                    "prev_structure_id": prev_last["structure_id"],
                    "next_structure_id": next_first["structure_id"],
                }
            )
    evidence = {
        "threshold_version": threshold_version,
        "enabled": bool(matched_pairs),
        "matched_pairs": matched_pairs,
    }
    return bool(matched_pairs), evidence
