"""基线 §10.5 / §19.1：review 单元、覆盖、次数上限、replacement 应用。"""

import unittest

from pdf2markdown import review

MD = b"# Title\n\nPara one.\n\nPara two.\n"

BLOCKS = [
    {"block_id": "b1", "reading_order": 0, "kind": "heading", "span": [0, 7]},
    {"block_id": "b2", "reading_order": 1, "kind": "paragraph", "span": [9, 18]},
    {"block_id": "b3", "reading_order": 2, "kind": "paragraph", "span": [20, 29]},
]

PAGES = [
    {"page_number": 1, "path": "/tmp/p1.png", "sha256": "a" * 64},
    {"page_number": 2, "path": "/tmp/p2.png", "sha256": "b" * 64},
]


def _review(block_id, verdict, text=None):
    return {
        "block_id": block_id,
        "verdict": verdict,
        "evidence_page": 1 if verdict in ("altered", "missing") else None,
        "evidence_bbox": [0, 0, 1, 1] if verdict in ("altered", "missing") else None,
        "replacement_text": text,
    }


class OneShotApplyTest(unittest.TestCase):
    """票 05：核验期间不改稿——所有裁决在原坐标系上一次性应用。"""

    def test_all_replacements_apply_against_the_original_coordinates(self):
        md = b"AAAA\n\nBBBB\n\nCCCC"
        blocks = [
            {"block_id": "b1", "span": [0, 4]},
            {"block_id": "b2", "span": [6, 10]},
            {"block_id": "b3", "span": [12, 16]},
        ]
        reviews = [
            {"block_id": "b1", "verdict": "altered", "evidence_page": None,
             "evidence_bbox": None, "replacement_text": "A"},
            {"block_id": "b2", "verdict": "altered", "evidence_page": None,
             "evidence_bbox": None, "replacement_text": "XX"},
            {"block_id": "b3", "verdict": "match", "evidence_page": None,
             "evidence_bbox": None, "replacement_text": None},
        ]
        revised, meta = review.apply_reviews(md, blocks, reviews)
        # 前一处替换缩短了文档，但后一处仍按原坐标定位——一次性应用不需要平移。
        self.assertEqual(revised, b"A\n\nXX\n\nCCCC")
        self.assertEqual(meta["lineage"]["b3"], "preserved")


class UnitTest(unittest.TestCase):
    def test_assignment_covers_all_pages(self):
        assignment = review.assign_blocks_to_pages(BLOCKS, 2)
        self.assertEqual(set(assignment), {1, 2})
        assigned = [i for ids in assignment.values() for i in ids]
        self.assertEqual(sorted(assigned), ["b1", "b2", "b3"])

    def test_units_have_boundaries(self):
        assignment = review.assign_blocks_to_pages(BLOCKS, 2)
        units = review.build_review_units(PAGES, BLOCKS, MD, assignment)
        self.assertEqual(len(units), 2)
        self.assertIsNone(units[0]["previous_boundary"])
        self.assertIsNone(units[-1]["next_boundary"])
        if units[0]["next_boundary"] is not None:
            self.assertEqual(units[0]["next_boundary"]["adjacent_page_number"], 2)
        fragments = [b["markdown_fragment"] for b in units[0]["blocks"]]
        self.assertTrue(any("# Title" in f for f in fragments))
        # 单元 ID 可重复生成
        units2 = review.build_review_units(PAGES, BLOCKS, MD, assignment)
        self.assertEqual(units, units2)


class BudgetTest(unittest.TestCase):
    def test_two_rounds_per_page(self):
        budget = review.ReviewBudget(2)
        budget.charge(1)
        budget.charge(1)
        with self.assertRaises(review.ReviewBudgetExceeded):
            budget.charge(1)

    def test_total_cap(self):
        budget = review.ReviewBudget(1)
        budget.charge(1)
        budget.charge(1)  # 首轮 + 一轮复核 = 2 次合法
        with self.assertRaises(review.ReviewBudgetExceeded):
            budget.charge(1)
        # 总量：2 页 → 总额 4
        budget = review.ReviewBudget(2)
        for page in (1, 1, 2, 2):
            budget.charge(page)
        with self.assertRaises(review.ReviewBudgetExceeded):
            budget.charge(2)


class CoverageTest(unittest.TestCase):
    def _unit(self):
        assignment = {1: ["b1", "b2", "b3"], 2: []}
        return review.build_review_units(PAGES, BLOCKS, MD, assignment)[0]

    def test_exact_coverage(self):
        unit = self._unit()
        reviews = [
            _review("b1", "match"),
            _review("b2", "altered", "Fixed text"),
            _review("b3", "extra"),
        ]
        self.assertTrue(review.check_coverage(unit, reviews))
        with self.assertRaises(review.ReviewIncomplete):
            review.check_coverage(unit, reviews[:2])  # 缺覆盖
        with self.assertRaises(review.ReviewIncomplete):
            review.check_coverage(unit, reviews + [_review("b1", "match")])  # 重复
        with self.assertRaises(review.ReviewIncomplete):
            review.check_coverage(unit, reviews[:2] + [_review("zz", "match")])  # 未知

    def test_replacement_constraints(self):
        for bad in (
            "<b>html</b>",
            "see https://example.com",
            "![img](x.png)",
            "<script>x</script>",
            "x" * (review.REPLACEMENT_MAX_BYTES + 1),
        ):
            with self.assertRaises(review.ReviewIncomplete):
                review.validate_replacement_text(bad)
        review.validate_replacement_text("纯文本与 **inline** 标记")

    def test_altered_requires_replacement(self):
        unit = self._unit()
        reviews = [
            _review("b1", "altered", None),
            _review("b2", "match"),
            _review("b3", "match"),
        ]
        with self.assertRaises(review.ReviewIncomplete):
            review.check_coverage(unit, reviews)


class ApplyTest(unittest.TestCase):
    def test_apply_replacements(self):
        reviews = [
            _review("b1", "match"),
            _review("b2", "altered", "Corrected paragraph."),
            _review("b3", "unreadable"),
        ]
        revised, meta = review.apply_reviews(MD, BLOCKS, reviews)
        self.assertIn(b"Corrected paragraph.", revised)
        self.assertNotIn(b"Para one.", revised)
        self.assertIn(b"Para two.", revised)  # 未改块保持
        self.assertEqual(meta["lineage"]["b2"], "replaced")
        self.assertEqual(meta["lineage"]["b1"], "preserved")
        self.assertEqual(meta["flags"]["unreadable"], ["b3"])

    def test_original_bytes_untouched(self):
        original = bytes(MD)
        reviews = [_review("b2", "missing", "Recovered.")]
        revised, _ = review.apply_reviews(MD, BLOCKS, reviews)
        self.assertEqual(MD, original)
        self.assertIn(b"Recovered.", revised)


if __name__ == "__main__":
    unittest.main()
