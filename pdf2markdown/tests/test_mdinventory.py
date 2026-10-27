"""基线 §10.5 / §19.1：Markdown 侧 block inventory（pandoc sourcepos）。"""

import hashlib
import unittest

from pdf2markdown import assets, mdinventory

try:
    PANDOC = assets.find_pandoc()
except assets.AssetError:
    PANDOC = None

SAMPLE = (
    "# Title\n"
    "\n"
    "Para one with **bold**.\n"
    "\n"
    "![figure](images/a.png)\n"
    "\n"
    "- item1\n"
    "- item2\n"
    "\n"
    "| a | b |\n"
    "|---|---|\n"
    "| 1 | 2 |\n"
    "\n"
    "```\ncode here\n```\n"
).encode("utf-8")


@unittest.skipUnless(PANDOC, "pandoc 3.10.1 not available")
class MdInventoryTest(unittest.TestCase):
    def test_kinds_and_order(self):
        blocks = mdinventory.parse_blocks(SAMPLE, PANDOC)
        kinds = [b["kind"] for b in blocks]
        self.assertEqual(
            kinds, ["heading", "paragraph", "image", "list_item", "table", "composite"]
        )
        orders = [b["reading_order"] for b in blocks]
        self.assertEqual(orders, list(range(len(blocks))))
        self.assertTrue(all(k in mdinventory.BLOCK_KINDS for k in kinds))

    def test_spans_cover_source_fragments(self):
        blocks = mdinventory.parse_blocks(SAMPLE, PANDOC)
        heading = blocks[0]
        self.assertIsNotNone(heading["span"])
        fragment = mdinventory.block_fragment(SAMPLE, heading)
        self.assertIn("# Title", fragment)
        table = next(b for b in blocks if b["kind"] == "table")
        self.assertIn("| a | b |", mdinventory.block_fragment(SAMPLE, table))
        # span 单调不减
        starts = [b["span"][0] for b in blocks if b["span"]]
        self.assertEqual(starts, sorted(starts))

    def test_no_trailing_newline_end_pos(self):
        # live 定型：文件末尾无换行时 pandoc 末端 sourcepos 为 one-past-end 行。
        md = b"# T\n\nfinal paragraph without newline"
        inv, _ = mdinventory.build_inventory(
            md, hashlib.sha256(md).hexdigest(), PANDOC
        )
        self.assertEqual(inv["block_count"], 2)

    def test_repeatable(self):
        inv1, digest1 = mdinventory.build_inventory(
            SAMPLE, hashlib.sha256(SAMPLE).hexdigest(), PANDOC
        )
        inv2, digest2 = mdinventory.build_inventory(
            SAMPLE, hashlib.sha256(SAMPLE).hexdigest(), PANDOC
        )
        self.assertEqual(digest1, digest2)
        self.assertEqual(inv1["block_count"], 6)
        self.assertEqual(
            inv1["schema_version"], "pdf2markdown.source-block-inventory/v1"
        )

    def test_invalid_markdown_never_crashes(self):
        blocks = mdinventory.parse_blocks(b"", PANDOC)
        self.assertEqual(blocks, [])


if __name__ == "__main__":
    unittest.main()
