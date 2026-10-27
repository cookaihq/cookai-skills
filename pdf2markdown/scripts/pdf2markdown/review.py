"""全量核验单元与响应应用（基线 §10.5、§8.5）。

- 按页切分核验单元：页码、页基准图路径、该页 block ID/Markdown 片段、
  前后相邻页边界（首尾为 null）。
- 次数上限：`页数 × 2`（每页首轮 + 至多一轮复核），绝对上限 4,000。
- 响应必须恰好覆盖单元内全部 block；`altered`/`missing` 携带
  `replacement_text`（≤65,536 bytes，禁 HTML 标签/脚本/URL/图片语法）。
- 程序按 block ID 把 replacement 应用回本地稿；宿主永远不能整篇提交。
"""

import re

from . import jcs, strictschema

REVIEW_ABSOLUTE_CAP = 4000
REPLACEMENT_MAX_BYTES = 65536

VERDICTS = ("match", "missing", "extra", "altered", "unreadable")
REPLACEMENT_VERDICTS = ("altered", "missing")

_FORBIDDEN_REPLACEMENT = re.compile(
    r"(<\s*[A-Za-z!/])|(https?://)|(!\[)|(<script)", re.IGNORECASE
)

SchemaError = strictschema.SchemaError


class ReviewIncomplete(ValueError):
    """覆盖缺失/未知 ID/重复/越界 → conversion-core/review_incomplete。"""


class ReviewBudgetExceeded(ValueError):
    """超出 页数×2 或绝对上限 → workflow/plan_limit_exceeded。"""


_IMAGE_TARGET = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_IMAGE_PAGE_PREFIX = re.compile(r"^(\d+)_")


def image_page_anchors(md_blocks, markdown_bytes, page_count):
    """图片块 → 源页的锚点（票 07）。

    Doc2X 把插图导出为 `images/<页索引>_<坐标…>.jpg`（record 24 实测），页索引从 0 起。
    这是确定性信号（文件名解析），不是语义匹配——刻意不复用已退役的 provenance 模糊映射。

    校验只做两条：锚点页索引**单调不减**、且**最大页索引 < page_count**。
    不比对「每页锚点数 ≤ 源清单该页图片数」——两者计数单位不同（源清单数 PDF 文本层
    image 对象，Doc2X 数自己切出的插图），record 24 第 1 页即为 1 vs 2，按那条会误降级。

    返回 {block_index: page_number}；任一校验不过返回 None（调用方降级）。
    """
    from . import mdinventory

    anchors = {}
    for index, block in enumerate(md_blocks):
        if block.get("kind") != "image" or not block.get("span"):
            continue
        fragment = mdinventory.block_fragment(markdown_bytes, block)
        target_match = _IMAGE_TARGET.search(fragment)
        if not target_match:
            continue
        name = target_match.group(1).rsplit("/", 1)[-1]
        match = _IMAGE_PAGE_PREFIX.match(name)
        if not match:
            continue
        page_index = int(match.group(1))
        if page_index >= page_count:
            return None  # 越界：命名约定与本文档不符，降级
        anchors[index] = page_index + 1
    if not anchors:
        return None
    ordered = [anchors[i] for i in sorted(anchors)]
    if any(b < a for a, b in zip(ordered, ordered[1:])):
        return None  # 非单调：块序与页序不对应，降级
    return anchors


def reconcile_omitted_images(inventory, md_blocks, markdown_bytes):
    """源结构清单里的图片块 vs 直转稿实际引用的图片（票 08）。

    只做**对账留证**，不建恢复通道：把「转换稿丢了源里的哪些图」变成可发现的事实。
    record 24 丢的两张是二维码，按 `qr-barcode` 规则本就是删除目标；真正的缺口在于
    若 Doc2X 丢的是正文插图（保护条款第 3 条要求保留），现协议既不能恢复也**不能发现**。

    返回 [{page_number, structure_id}]，按页码 + structure_id 排序。
    """
    anchored = set()
    for index, block in enumerate(md_blocks):
        if block.get("kind") != "image" or not block.get("span"):
            continue
        from . import mdinventory

        fragment = mdinventory.block_fragment(markdown_bytes, block)
        target_match = _IMAGE_TARGET.search(fragment)
        if not target_match:
            continue
        name = target_match.group(1).rsplit("/", 1)[-1]
        match = _IMAGE_PAGE_PREFIX.match(name)
        if match:
            anchored.add((int(match.group(1)) + 1, name))
    counts = {}
    for page_number, _name in anchored:
        counts[page_number] = counts.get(page_number, 0) + 1
    omitted = []
    for page in inventory.get("pages") or []:
        page_number = page["page_number"]
        images = [b for b in page.get("blocks") or [] if b.get("kind") == "image"]
        converted = counts.get(page_number, 0)
        # 计数单位不同（源侧是 PDF 文本层 image 对象，Doc2X 是自己切出的插图），
        # 所以只在「源侧有图而转换稿该页一张都没有」时才判定为遗漏——保守留证，
        # 不制造噪声。
        if images and converted == 0:
            for block in images:
                omitted.append(
                    {"page_number": page_number, "structure_id": block["structure_id"]}
                )
    return sorted(omitted, key=lambda o: (o["page_number"], o["structure_id"]))


def assign_blocks_to_pages(md_blocks, page_count, markdown_bytes=None):
    """把 Markdown block 分配到源页：图片锚点 + 段内插值，失败降级为比例启发式。

    分配只影响核验单元的组织方式，不影响安全性——错配由 reviewer 以
    missing/extra verdict 表达并在复核轮（票 06）修正。

    已知残留缺口：末锚点之后的块只能按尾部比例插值。record 24 第 4 页 Doc2X 零图片，
    该页的块即落在此区间，其错配由复核轮兜底。
    """
    if page_count <= 0:
        raise ReviewIncomplete("no pages to review")
    assignment = {page: [] for page in range(1, page_count + 1)}
    if not md_blocks:
        return assignment
    anchors = (
        image_page_anchors(md_blocks, markdown_bytes, page_count)
        if markdown_bytes
        else None
    )
    if anchors:
        pages = _pages_by_anchor_interpolation(
            md_blocks, anchors, page_count, len(markdown_bytes)
        )
    else:
        pages = _pages_by_byte_ratio(md_blocks, page_count)
    for block, page in zip(md_blocks, pages):
        assignment[page].append(block["block_id"])
    return assignment


def _pages_by_byte_ratio(md_blocks, page_count):
    """降级路径：按块起点字节位置占全文比例线性推算（v1 行为，逐字保留）。"""
    total_span = max((b["span"][1] for b in md_blocks if b["span"]), default=0) or 1
    pages = []
    for block in md_blocks:
        start = block["span"][0] if block["span"] else 0
        pages.append(min(page_count, 1 + int(start * page_count / max(total_span, 1))))
    return pages


def _pages_by_anchor_interpolation(md_blocks, anchors, page_count, total_bytes):
    """按**字节位置**做分段线性映射，锚点即已知的「字节位置 → 页」控制点。

    刻意不按块序等分：块的字节长度差异极大（一个标题 vs 一大段正文），
    按块序插值会把两个锚点之间的所有块压向前一个锚点所在页（record 24 实测：
    第 1 页会被塞进 37 个块，而它实际是只有几块的封面）。
    """
    def block_start(block):
        return block["span"][0] if block.get("span") else 0

    control = [(0, 1)]
    for index in sorted(anchors):
        control.append((block_start(md_blocks[index]), anchors[index]))
    control.append((max(total_bytes, control[-1][0] + 1), page_count))

    pages = []
    for index, block in enumerate(md_blocks):
        if index in anchors:
            pages.append(anchors[index])
            continue
        position = block_start(block)
        page = page_count
        for (low_byte, low_page), (high_byte, high_page) in zip(control, control[1:]):
            if position <= high_byte:
                if high_byte == low_byte or high_page == low_page:
                    page = low_page
                else:
                    ratio = (position - low_byte) / float(high_byte - low_byte)
                    page = low_page + int(round(ratio * (high_page - low_page)))
                break
        pages.append(min(page_count, max(1, page)))
    return pages


def build_review_units(page_records, md_blocks, markdown_bytes, assignment):
    """构造全部页的核验单元（含边界）；返回按页码排序的单元列表。"""
    from . import mdinventory

    blocks_by_id = {b["block_id"]: b for b in md_blocks}
    units = []
    page_count = len(page_records)
    for record in sorted(page_records, key=lambda r: r["page_number"]):
        page_number = record["page_number"]
        block_ids = assignment.get(page_number, [])
        blocks = [
            {
                "block_id": block_id,
                "markdown_fragment": mdinventory.block_fragment(
                    markdown_bytes, blocks_by_id[block_id]
                ),
            }
            for block_id in block_ids
        ]

        def boundary(adjacent_number):
            if adjacent_number < 1 or adjacent_number > page_count:
                return None
            adjacent_ids = assignment.get(adjacent_number, [])
            edge_ids = adjacent_ids[:1] if adjacent_number > page_number else adjacent_ids[-1:]
            adjacent_record = next(
                r for r in page_records if r["page_number"] == adjacent_number
            )
            return {
                "adjacent_page_number": adjacent_number,
                "adjacent_page_crop_path": adjacent_record["path"],
                "boundary_block_ids": edge_ids,
                "boundary_markdown_fragments": [
                    mdinventory.block_fragment(markdown_bytes, blocks_by_id[i])
                    for i in edge_ids
                ],
            }

        unit_id = jcs.sha256_canonical(
            {"page_number": page_number, "block_ids": block_ids}
        )[:32]
        units.append(
            {
                "review_unit_id": unit_id,
                "page_number": page_number,
                "page_reference_path": record["path"],
                "previous_boundary": boundary(page_number - 1),
                "next_boundary": boundary(page_number + 1),
                "blocks": blocks,
            }
        )
    return units


class ReviewBudget:
    """次数预算：每页首轮 + 至多一轮复核；绝对上限 4,000（§10.5）。

    票 06 起接入生产：`page_count` 不再由调用方传入，而是随状态持久化——
    预算跨命令调用累积，第三轮在结构上不可表达。
    """

    def __init__(self, page_count=None):
        self.page_count = page_count
        self.rounds_by_page = {}
        self.total = 0

    def charge(self, page_number):
        key = str(page_number)
        rounds = self.rounds_by_page.get(key, 0)
        if rounds >= 2:
            raise ReviewBudgetExceeded(
                "page %s exceeded first round + one recheck" % key
            )
        if self.page_count is not None and self.total >= min(
            self.page_count * 2, REVIEW_ABSOLUTE_CAP
        ):
            raise ReviewBudgetExceeded("review action budget exhausted")
        if self.total >= REVIEW_ABSOLUTE_CAP:
            raise ReviewBudgetExceeded("review action budget exhausted")
        self.rounds_by_page[key] = rounds + 1
        self.total += 1

    def rounds_for(self, page_number):
        return self.rounds_by_page.get(str(page_number), 0)

    def as_state(self):
        return {"rounds_by_page": dict(self.rounds_by_page), "total": self.total}

    @classmethod
    def from_state(cls, state, page_count=None):
        budget = cls(page_count)
        if state:
            budget.rounds_by_page = dict(state.get("rounds_by_page") or {})
            budget.total = state.get("total") or 0
        return budget


def build_recheck_units(first_round_units, phase, page_records, md_blocks, markdown_bytes):
    """复核单元（票 06）：只复核首轮出现过替换的页，且只含被替换的块。

    - 触发：该页首轮有 altered/missing 裁决；零替换的页不复核。
    - 内容：块片段取**首轮的 replacement_text**（核验期间不改稿，故不能从文档里取）。
    - 终轮：每页至多两轮由 `ReviewBudget` 保证，此处不再产生第三轮单元。
    - `preserved` 块不重审——首轮已对照页图确认过，重审只烧预算不加信息。
    """
    accumulator = ReviewAccumulator.from_state(phase.get("accumulated"))
    replaced = set(accumulator.replaced_block_ids())
    if not replaced:
        return []
    budget = ReviewBudget.from_state(phase.get("budget"))
    merged = {item["block_id"]: item for item in accumulator.reviews()}
    records_by_page = {r["page_number"]: r for r in page_records}
    blocks_by_id = {b["block_id"]: b for b in md_blocks}
    units = []
    for unit in first_round_units:
        page_number = unit["page_number"]
        page_replaced = [
            b["block_id"] for b in unit["blocks"] if b["block_id"] in replaced
        ]
        if not page_replaced:
            continue
        if budget.rounds_for(page_number) >= 2:
            continue  # 该页复核轮已用掉：复核是终轮
        record = records_by_page[page_number]
        blocks = [
            {
                "block_id": block_id,
                "markdown_fragment": merged[block_id]["replacement_text"],
            }
            for block_id in page_replaced
            if blocks_by_id.get(block_id) is not None
        ]
        unit_id = jcs.sha256_canonical(
            {"page_number": page_number, "block_ids": page_replaced, "round": 2}
        )[:32]
        units.append(
            {
                "review_unit_id": unit_id,
                "page_number": page_number,
                "page_reference_path": record["path"],
                "previous_boundary": unit["previous_boundary"],
                "next_boundary": unit["next_boundary"],
                "blocks": blocks,
            }
        )
    return units


def validate_replacement_text(text, ctx="replacement_text"):
    if not isinstance(text, str):
        raise ReviewIncomplete("%s: string required" % ctx)
    if len(text.encode("utf-8")) > REPLACEMENT_MAX_BYTES:
        raise ReviewIncomplete("%s: exceeds 65536 bytes" % ctx)
    if _FORBIDDEN_REPLACEMENT.search(text):
        raise ReviewIncomplete(
            "%s: HTML tags, scripts, URLs and image syntax are forbidden" % ctx
        )
    return text


def check_coverage(unit, reviews):
    """响应必须恰好覆盖单元内全部 block（未知/重复/缺少 → review_incomplete）。"""
    unit_ids = [b["block_id"] for b in unit["blocks"]]
    seen = []
    for review in reviews:
        block_id = review["block_id"]
        if block_id not in unit_ids:
            raise ReviewIncomplete("review references unknown block id")
        if block_id in seen:
            raise ReviewIncomplete("duplicate review for block id")
        if review["verdict"] not in VERDICTS:
            raise ReviewIncomplete("verdict outside closed set")
        if review["verdict"] in REPLACEMENT_VERDICTS:
            if review["replacement_text"] is None:
                raise ReviewIncomplete("altered/missing must carry replacement_text")
            validate_replacement_text(review["replacement_text"])
        seen.append(block_id)
    missing = set(unit_ids) - set(seen)
    if missing:
        raise ReviewIncomplete("coverage missing for %d blocks" % len(missing))
    return True


def apply_reviews(markdown_bytes, md_blocks, reviews):
    """把 altered/missing 的 replacement 一次性应用回本地稿。

    返回 (revised_bytes, lineage)；lineage: {block_id: relation}，
    relation ∈ {preserved, replaced}；extra/unreadable 记录 verdict 供上游
    生成修订裁决（不在此处删除内容）。全部编辑通过 span 完成、零部分提交。
    """
    blocks_by_id = {b["block_id"]: b for b in md_blocks}
    edits = []
    lineage = {}
    flags = {"extra": [], "unreadable": []}
    for review in reviews:
        block = blocks_by_id.get(review["block_id"])
        if block is None:
            raise ReviewIncomplete("review references unknown block id")
        verdict = review["verdict"]
        if verdict in REPLACEMENT_VERDICTS:
            if block["span"] is None:
                raise ReviewIncomplete("block without span cannot be replaced")
            validate_replacement_text(review["replacement_text"])
            edits.append((tuple(block["span"]), review["replacement_text"]))
            lineage[review["block_id"]] = "replaced"
        else:
            lineage[review["block_id"]] = "preserved"
            if verdict in ("extra", "unreadable"):
                flags[verdict].append(review["block_id"])
    # 编辑区间不得重叠；一次性按序应用（零部分提交）。
    ordered = sorted(edits, key=lambda e: e[0])
    previous_end = -1
    for (start, end), _text in ordered:
        if start < previous_end:
            raise ReviewIncomplete("overlapping replacement spans")
        previous_end = end
    out = bytearray()
    cursor = 0
    applied = []
    for (start, end), text in ordered:
        encoded = text.encode("utf-8")
        out += markdown_bytes[cursor:start]
        out += encoded
        cursor = end
        applied.append((start, end, len(encoded)))
    out += markdown_bytes[cursor:]
    return bytes(out), {"lineage": lineage, "flags": flags, "edits": applied}


class ReviewAccumulator:
    """跨核验单元累积裁决；全部单元完成后一次性应用（§10.5，票 05）。

    以 `block_id` 为键、后写覆盖先写——复核轮对同一块的再裁决覆盖首轮裁决。
    **不能用 append 列表**：同一块两条 replacement 会让 `apply_reviews` 判定
    span 重叠并零部分提交，整篇失败。

    核验期间不写中间态文档，因此所有 span 始终处在原始直转稿的坐标系里，
    「替换后平移 span」这一步不复存在（record 24 的 4 组重叠即源于此）。
    """

    def __init__(self):
        self._by_block = {}
        self._recheck_flags = {}

    def absorb(self, reviews, recheck=False):
        """吸收一轮裁决。

        首轮：直接记入。
        复核轮：审的对象是**首轮替换后的文本**，所以 `match` 的含义是「该替换是对的」，
        而不是「原块无需替换」——此时必须保留首轮的 replacement，不能覆盖掉。
        只有复核轮再次给出 replacement（altered/missing）才取代首轮结果。
        """
        for item in reviews:
            block_id = item["block_id"]
            previous = self._by_block.get(block_id)
            if recheck and previous is not None:
                # `match` = 「上一轮那个替换是对的」，保留即可。
                if item["verdict"] == "match":
                    continue
                # extra / unreadable 说的是「这块不该在页面上 / 读不出来」，与「这块的
                # 文字该怎么写」是两件事。若首轮已给出替换，**保留替换文本**、另记 flag：
                # 丢掉替换会让该块退回首轮已判定为错的原始字节，等于用一个新缺陷
                # 覆盖旧缺陷。flag 交由上游生成修订裁决（extra 的处置本就不在核验阶段）。
                if (
                    item["verdict"] in ("extra", "unreadable")
                    and previous["verdict"] in REPLACEMENT_VERDICTS
                ):
                    self._recheck_flags[block_id] = item["verdict"]
                    continue
            self._by_block[block_id] = item

    def recheck_flags(self):
        """复核轮给出、但因保留首轮替换而未体现在 verdict 里的标记。"""
        return {
            key: sorted(
                block_id
                for block_id, verdict in self._recheck_flags.items()
                if verdict == key
            )
            for key in ("extra", "unreadable")
        }

    def reviews(self):
        """按 block_id 排序返回，保证同一批裁决产出字节级确定的结果。"""
        return [self._by_block[key] for key in sorted(self._by_block)]

    def replaced_block_ids(self):
        return sorted(
            block_id
            for block_id, item in self._by_block.items()
            if item["verdict"] in REPLACEMENT_VERDICTS
        )

    def as_state(self):
        return {"by_block": self._by_block, "recheck_flags": self._recheck_flags}

    @classmethod
    def from_state(cls, state):
        acc = cls()
        if state:
            acc._by_block = dict(state.get("by_block") or {})
            acc._recheck_flags = dict(state.get("recheck_flags") or {})
        return acc
