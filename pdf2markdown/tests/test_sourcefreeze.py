"""基线 §10.2 / §19.1：源冻结、页基准、结构预检（Python >= 3.10 + 锁定依赖）。"""

import hashlib
import os
import shutil
import tempfile
import unittest

from pdf2markdown import sourcefreeze


def _make_pdf(path, pages=2, with_table=False, table_at_edges=False):
    import fitz

    doc = fitz.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), "Page %d heading text" % (i + 1), fontsize=18)
        page.insert_text((72, 200), "Body paragraph on page %d." % (i + 1), fontsize=11)
        if with_table:
            # 画一个简单表格（横竖线 + 单元格文字）供 find_tables 检测。
            if table_at_edges:
                top = 700 if i == 0 else 10
                bottom = 838 if i == 0 else 120
            else:
                top, bottom = 400, 500
            for y in (top, (top + bottom) / 2, bottom):
                page.draw_line((72, y), (500, y))
            for x in (72, 286, 500):
                page.draw_line((x, top), (x, bottom))
            page.insert_text((80, top + 30), "cell A", fontsize=9)
            page.insert_text((300, top + 30), "cell B", fontsize=9)
    doc.save(path)
    doc.close()


class SourceFreezeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p2mvi-core-")
        os.chmod(self.dir, 0o700)
        self.pdf = os.path.join(self.dir, "in.pdf")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_freeze_and_verify(self):
        _make_pdf(self.pdf)
        dest = os.path.join(self.dir, "frozen", "source.pdf")
        sha, count = sourcefreeze.freeze_source(self.pdf, dest)
        with open(self.pdf, "rb") as fh:
            raw = fh.read()
        self.assertEqual(sha, hashlib.sha256(raw).hexdigest())
        self.assertEqual(count, len(raw))
        sourcefreeze.verify_frozen_source(dest, sha, count)
        with self.assertRaises(sourcefreeze.SourceFreezeError):
            sourcefreeze.verify_frozen_source(dest, "0" * 64, count)

    def test_freeze_rejects_non_pdf(self):
        bad = os.path.join(self.dir, "bad.bin")
        with open(bad, "wb") as fh:
            fh.write(b"not a pdf")
        with self.assertRaises(sourcefreeze.SourceFreezeError):
            sourcefreeze.freeze_source(bad, os.path.join(self.dir, "f", "source.pdf"))

    def test_render_page_references_deterministic(self):
        _make_pdf(self.pdf, pages=2)
        dest = os.path.join(self.dir, "frozen", "source.pdf")
        sourcefreeze.freeze_source(self.pdf, dest)
        out1 = os.path.join(self.dir, "pages1")
        out2 = os.path.join(self.dir, "pages2")
        records1 = sourcefreeze.render_page_references(dest, out1)
        records2 = sourcefreeze.render_page_references(dest, out2)
        self.assertEqual(len(records1), 2)
        for r1, r2 in zip(records1, records2):
            self.assertEqual(r1["sha256"], r2["sha256"])  # 同源同参数逐页同 SHA
            self.assertEqual(r1["width_px"], r2["width_px"])
            self.assertEqual((r1["dpi"], r1["render_policy"]), (144, "page-reference-render/v1"))
            self.assertLessEqual(r1["width_px"], sourcefreeze.PAGE_MAX_EDGE_PX)
            with open(r1["path"], "rb") as fh:
                self.assertEqual(
                    hashlib.sha256(fh.read()).hexdigest(), r1["sha256"]
                )

    def test_inventory_repeatable_and_kinds(self):
        _make_pdf(self.pdf, pages=2, with_table=True)
        dest = os.path.join(self.dir, "frozen", "source.pdf")
        sha, _ = sourcefreeze.freeze_source(self.pdf, dest)
        records = sourcefreeze.render_page_references(dest, os.path.join(self.dir, "pages"))
        inv1, digest1 = sourcefreeze.build_source_structure_inventory(dest, sha, records)
        inv2, digest2 = sourcefreeze.build_source_structure_inventory(dest, sha, records)
        self.assertEqual(digest1, digest2)  # 可重复生成
        self.assertEqual(inv1["schema_version"], sourcefreeze.INVENTORY_SCHEMA_VERSION)
        self.assertEqual(inv1["page_count"], 2)
        kinds = {b["kind"] for p in inv1["pages"] for b in p["blocks"]}
        self.assertTrue(kinds <= set(sourcefreeze.BLOCK_KINDS))
        for page in inv1["pages"]:
            orders = [b["reading_order"] for b in page["blocks"]]
            self.assertEqual(orders, list(range(len(orders))))
            for block in page["blocks"]:
                x0, y0, x1, y1 = block["bbox"]
                self.assertTrue(0 <= x0 <= x1 <= 1)
                self.assertTrue(0 <= y0 <= y1 <= 1)

    def test_cross_page_table_precheck(self):
        # 表格贴近页界：前页末表 y1>=0.98 且后页首表 y0<=0.02 → 启用
        _make_pdf(self.pdf, pages=2, with_table=True, table_at_edges=True)
        dest = os.path.join(self.dir, "frozen", "source.pdf")
        sha, _ = sourcefreeze.freeze_source(self.pdf, dest)
        records = sourcefreeze.render_page_references(dest, os.path.join(self.dir, "pages"))
        inv, _ = sourcefreeze.build_source_structure_inventory(dest, sha, records)
        has_tables = any(
            b["kind"] == "table" for p in inv["pages"] for b in p["blocks"]
        )
        enabled, evidence = sourcefreeze.cross_page_table_precheck(inv)
        self.assertEqual(evidence["threshold_version"], "cross_page_table_geometry/v1")
        if has_tables:
            self.assertTrue(enabled)
            self.assertTrue(evidence["matched_pairs"])
        # 中部表格：不满足阈值 → 关闭
        pdf2 = os.path.join(self.dir, "mid.pdf")
        _make_pdf(pdf2, pages=2, with_table=True, table_at_edges=False)
        dest2 = os.path.join(self.dir, "frozen2", "source.pdf")
        sha2, _ = sourcefreeze.freeze_source(pdf2, dest2)
        records2 = sourcefreeze.render_page_references(dest2, os.path.join(self.dir, "pages-mid"))
        inv2, _ = sourcefreeze.build_source_structure_inventory(dest2, sha2, records2)
        enabled2, evidence2 = sourcefreeze.cross_page_table_precheck(inv2)
        self.assertFalse(enabled2)
        self.assertEqual(evidence2["matched_pairs"], [])


if __name__ == "__main__":
    unittest.main()
