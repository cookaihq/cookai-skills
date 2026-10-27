"""票 06 端到端：有替换的页确实跑第二轮，无替换的页不跑。

record 24 定型：91 个块里 36 个被替换，却总共只跑了 4 次核验（每页一次）——
`ReviewBudget` 从未接入生产。本测试直接驱动 Core pipeline，数每页的核验次数。
"""

import os
import shutil
import tempfile
import unittest

from pdf2markdown import assets, pipeline

try:
    PANDOC = assets.find_pandoc()
except assets.AssetError:
    PANDOC = None

# 两页文档：第 1 页的段落会被判 altered，第 2 页全部 match。
MARKDOWN = (
    "# Title\n\n"
    "First page paragraph that the reviewer will rewrite.\n\n"
    "![f](images/0_1_2_3_4_0.jpg)\n\n"
    "Second page paragraph left untouched.\n\n"
    "![g](images/1_1_2_3_4_0.jpg)\n"
).encode("utf-8")

ALTERED_MARKER = "rewrite"


@unittest.skipUnless(PANDOC, "pandoc 3.10.1 not available")
class RecheckRoundE2ETest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="p2mvi-recheck-")
        os.chmod(self.dir, 0o700)
        self.work = os.path.join(self.dir, "work")
        direct_dir = os.path.join(self.work, "03-direct", "tree")
        os.makedirs(direct_dir)
        self.direct_path = os.path.join(direct_dir, "output.md")
        with open(self.direct_path, "wb") as fh:
            fh.write(MARKDOWN)
        self.pages_seen = []

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _pipe(self):
        from pdf2markdown import mdinventory

        pipe = pipeline.ConversionPipeline(self.work, {"pandoc_path": PANDOC})
        blocks = mdinventory.parse_blocks(MARKDOWN, PANDOC)
        pipe._set_phase(
            "source",
            {
                "page_count": 2,
                "pages": [
                    {"page_number": 1, "path": os.path.join(self.work, "p1.png")},
                    {"page_number": 2, "path": os.path.join(self.work, "p2.png")},
                ],
            },
        )
        pipe._set_phase(
            "direct",
            {
                "primary_path": self.direct_path,
                "md_blocks": blocks,
                "staging": os.path.dirname(self.direct_path),
                "sha256": "0" * 64,
                "byte_count": len(MARKDOWN),
                "members": [],
            },
        )
        return pipe

    def _review(self, unit, rewrite):
        """第一轮把含标记的块判 altered；复核轮一律 match。"""
        reviews = []
        for block in unit["blocks"]:
            altered = rewrite and ALTERED_MARKER in block["markdown_fragment"]
            reviews.append(
                {
                    "block_id": block["block_id"],
                    "verdict": "altered" if altered else "match",
                    "evidence_page": unit["page_number"] if altered else None,
                    "evidence_bbox": [0, 0, 1, 1] if altered else None,
                    "replacement_text": "First page paragraph, corrected."
                    if altered
                    else None,
                }
            )
        return reviews

    def _drive_review(self, max_rounds=12):
        pipe = self._pipe()
        seen_units = set()
        rounds = 0
        while True:
            rounds += 1
            self.assertLess(rounds, max_rounds)
            out = pipe.review_requests()
            if out["status"] == "artifacts_prepared":
                return pipe, out
            unit = out["request"]
            first_time = unit["review_unit_id"] not in seen_units
            seen_units.add(unit["review_unit_id"])
            self.pages_seen.append(unit["page_number"])
            result = pipe.record_review_response(unit, self._review(unit, first_time))
            self.assertEqual(result["status"], "artifacts_prepared", result)

    def test_replaced_page_is_reviewed_twice_clean_page_once(self):
        self._drive_review()
        self.assertEqual(
            self.pages_seen.count(1), 2, "有替换的页必须跑首轮 + 复核轮"
        )
        self.assertEqual(
            self.pages_seen.count(2), 1, "零替换的页不复核"
        )

    def test_verified_markdown_is_written_only_after_all_rounds(self):
        pipe, out = self._drive_review()
        phase = out["review"]
        self.assertTrue(phase["verified_path"])
        with open(phase["verified_path"], "rb") as fh:
            verified = fh.read()
        self.assertIn(b"First page paragraph, corrected.", verified)
        self.assertIn(b"Second page paragraph left untouched.", verified)

    def test_no_intermediate_document_exists_before_the_last_round(self):
        """核验期间不改稿：中途 04-review/verified.md 不应存在（票 05）。"""
        pipe = self._pipe()
        out = pipe.review_requests()
        self.assertEqual(out["status"], "host_action_requested")
        unit = out["request"]
        pipe.record_review_response(unit, self._review(unit, True))
        self.assertFalse(
            os.path.exists(os.path.join(self.work, "04-review", "verified.md")),
            "首轮结束就落盘 = 又回到了会产生 span 平移的结构",
        )


if __name__ == "__main__":
    unittest.main()
