"""Conversion core 流水线（基线 §10.6 `prepare_source` / `convert_and_process`）。

Core 只消费 workflow 分配的身份与严格合同；不分配 ID、不写 Base/OSS/
Webhook 全局状态。宿主逐页核验请求通过
`host_action_requested` 结果交给 workflow 执行并回填；Core 状态持久化在
attempt 工作目录的 `core-state.json`（canonical JSON、原子替换），因此
跨进程可重入：每次调用从状态文件继续，已完成阶段不重做。

Core result 只使用 `artifacts_prepared`、`host_action_requested`、
`completed`、`blocked`、`failed`。
"""

import hashlib
import json
import os
import shutil

from . import fsatomic, jcs
from . import (
    assets,
    doc2x,
    localize,
    mdinventory,
    review,
    sourcefreeze,
    zipsafe,
)

STATE_FILE = ".state/core-state.json"

RESULT_STATUSES = (
    "artifacts_prepared",
    "host_action_requested",
    "completed",
    "blocked",
    "failed",
)


class CoreResult(dict):
    """{status, reason?, request?, artifacts?}——status 闭合五值。"""

    def __init__(self, status, **payload):
        if status not in RESULT_STATUSES:
            raise ValueError("invalid core result status: %r" % (status,))
        super().__init__(status=status, **payload)


def _blocked(raw_reason, summary, successor_eligible=True):
    """blocked 结果；`successor_eligible=False` 表示确定无效、禁止付费重试。

    同一个 `conversion_result_invalid` 在 §10.4 下有两种命运：结果**不可得**
    （下载 4xx、重试穷尽）值得按 §9.3 再买一次转换，结果**确定无效**（外链
    scheme 不可本地化、字节嗅探落在闭合映射外）再买多少次都会产出同样的东西。
    delivery 的 `_stop_or_start_successor` 只看 raw reason，分不出这两者，
    因此把判据放在产生它的这一层——与结果 ZIP 分诊把 `INVALID` 和
    `UNAVAILABLE` 分成两个 kind 是同一处理。
    """
    return CoreResult(
        "blocked",
        reason={"raw_reason": raw_reason, "masked_summary": summary},
        successor_eligible=successor_eligible,
    )


def _empty_review_phase():
    """核验阶段状态：完成的单元、累积裁决、次数预算（票 05/06）。"""
    return {
        "completed_units": [],
        "accumulated": {"by_block": {}},
        "budget": {"rounds_by_page": {}, "total": 0},
        "lineage": {},
        "flags": {"extra": [], "unreadable": []},
    }


def _failed(raw_reason, summary):
    return CoreResult("failed", reason={"raw_reason": raw_reason, "masked_summary": summary})


class ConversionPipeline:
    """普通转换：源冻结 → 页基准 → 预检 → Doc2X → ZIP → inventory →
    逐页核验 → 修改页复核 → 一次性修订。

    services 必须提供：
      doc2x_transport(request) -> (status_code, raw_body)
      doc2x_base_url: str
      pandoc_path: str
      sleep/monotonic（可选，转发给 PollWindow）
    """

    def __init__(self, work_dir, services, read_only=False):
        self.work_dir = work_dir
        self.services = services
        directory = fsatomic.check_safe_dir if read_only else fsatomic.ensure_private_dir
        directory(work_dir, "core work dir")
        directory(os.path.join(work_dir, ".state"), "core state dir")
        self.state_path = os.path.join(work_dir, STATE_FILE)
        self.state = self._load_state()

    # ---- 状态持久化 ----

    def _load_state(self):
        if not os.path.lexists(self.state_path):
            return {"schema_version": "pdf2markdown.core-state/v1", "phases": {}}
        fsatomic.check_safe_regular_file(self.state_path, "core state")
        with open(self.state_path, "rb") as fh:
            state = jcs.loads_strict(fh.read())
        if not isinstance(state, dict) or state.get("schema_version") != "pdf2markdown.core-state/v1" or not isinstance(state.get("phases"), dict):
            raise ValueError("invalid core state")
        return state

    def _save_state(self):
        fsatomic.atomic_replace_bytes(
            self.state_path, jcs.canonicalize(self.state), "core state"
        )

    def _phase(self, name):
        return self.state["phases"].get(name)

    def _set_phase(self, name, payload):
        self.state["phases"][name] = payload
        self._save_state()

    # ---- 阶段 ----

    def prepare_source(self, input_pdf_path):
        """源冻结 + 页基准 + 结构 inventory + 跨页表预检（§10.2/§10.3）。"""
        if self._phase("source") is None:
            frozen = os.path.join(self.work_dir, "01-source", "source.pdf")
            try:
                sha, byte_count = sourcefreeze.freeze_source(input_pdf_path, frozen)
                pages = sourcefreeze.render_page_references(
                    frozen, os.path.join(self.work_dir, "02-pages")
                )
                inventory, inventory_sha = sourcefreeze.build_source_structure_inventory(
                    frozen, sha, pages
                )
                enabled, precheck = sourcefreeze.cross_page_table_precheck(inventory)
            except sourcefreeze.SourceFreezeError as exc:
                return _blocked("source_invalid", str(exc))
            inventory_path = os.path.join(self.work_dir, "source-structure-inventory.json")
            fsatomic.atomic_write_bytes(
                inventory_path, jcs.canonicalize(inventory), "structure inventory"
            )
            self._set_phase(
                "source",
                {
                    "frozen_path": frozen,
                    "sha256": sha,
                    "byte_count": byte_count,
                    "page_count": len(pages),
                    "pages": pages,
                    "inventory_path": inventory_path,
                    "inventory_sha256": inventory_sha,
                    "cross_page_tables": enabled,
                    "precheck_evidence": precheck,
                },
            )
        return CoreResult("artifacts_prepared", source=self._phase("source"))

    POLL_TRACE_FILE = "doc2x-poll-trace.jsonl"

    def _poll_trace(self):
        """轮询痕迹回调：每次 GET 追加一行 JSONL 到 attempt 工作目录。

        纯本地诊断——不进事件流、不进 Base、不进 stdout。写入失败一律吞掉：
        一个诊断文件绝不能把已付费的轮询主流程带下水。
        """
        path = os.path.join(self.work_dir, self.POLL_TRACE_FILE)

        def trace(record):
            try:
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                fd = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                try:
                    os.write(fd, line.encode("utf-8"))
                finally:
                    os.close(fd)
            except Exception:
                pass

        return trace

    LOCALIZATION_TRACE_FILE = "localization-trace.jsonl"

    def _write_localization_trace(self, outcome):
        """本地化痕迹：每个外链目标一行 JSONL，写 attempt 工作目录。

        纯本地诊断——不进事件流、不进 Base、不进 stdout（§16 URL 日志策略：
        URL 只记 origin 与 SHA-256 指纹，不记完整链接）。写入失败一律吞掉：
        一个诊断文件绝不能把已付费的采用流程带下水。
        """
        records = outcome.get("records") or []
        if not records:
            return
        path = os.path.join(self.work_dir, self.LOCALIZATION_TRACE_FILE)
        try:
            lines = "".join(
                json.dumps(
                    dict(record, outcome_kind=outcome["kind"]),
                    ensure_ascii=False, sort_keys=True,
                ) + "\n"
                for record in records
            )
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, lines.encode("utf-8"))
            finally:
                os.close(fd)
        except Exception:
            pass

    def run_doc2x(self, immutable_source_url):
        """一次创建 + 同 task 轮询（§10.3）；重入时只轮询既有 task。"""
        source = self._phase("source")
        if source is None:
            return _failed("action_contract_invalid", "source phase not prepared")
        phase = self._phase("doc2x") or {}
        if phase.get("result_url"):
            return CoreResult("artifacts_prepared", doc2x=phase)
        if phase.get("error"):
            return _blocked(phase["error"]["raw_reason"], phase["error"]["masked_summary"])
        try:
            if "task_id" not in phase:
                if phase.get("posted"):
                    # §9.3：POST 已发出且无 task ID → submission_unknown；
                    # 只有新 paid successor 才能再次 POST，重入绝不重发。
                    return _failed(
                        "submission_unknown",
                        "doc2x create previously posted without provable acceptance",
                    )
                request = doc2x.build_create_request(
                    self.services["doc2x_base_url"],
                    immutable_source_url,
                    source["page_count"],
                    source["sha256"],
                    source["cross_page_tables"],
                )
                # 付费 POST 前先持久化 posted 事实（越过发送边界即不可重放）。
                self._set_phase("doc2x", {"posted": True})
                status_code, raw = self.services["doc2x_transport"](request)
                created = doc2x.validate_create_response(status_code, raw)
                phase = {"task_id": created["task_id"], "posted": True}
                self._set_phase("doc2x", phase)
            window = doc2x.PollWindow(
                monotonic=self.services.get("monotonic"),
                sleep=self.services.get("sleep"),
                trace=self._poll_trace(),
            )
            outcome = window.poll(
                self.services["doc2x_transport"],
                self.services["doc2x_base_url"],
                phase["task_id"],
            )
        except doc2x.Doc2XReason as exc:
            if exc.raw_reason == "conversion_result_invalid" and not phase.get("task_id"):
                self._set_phase("doc2x", {"posted": True, "error": {"raw_reason": exc.raw_reason, "masked_summary": exc.masked_summary}})
            status = "failed" if exc.raw_reason == "submission_unknown" else "blocked"
            builder = _failed if status == "failed" else _blocked
            return builder(exc.raw_reason, exc.masked_summary)
        phase["result_url"] = outcome["result_url"]
        self._set_phase("doc2x", phase)
        return CoreResult("artifacts_prepared", doc2x=phase)

    def adopt_result_zip(self, zip_path):
        """ZIP 安全采用 + 直接稿选定 + Markdown block inventory（§10.4/§10.5）。"""
        if self._phase("direct") is None:
            staging = os.path.join(self.work_dir, "03-direct", "tree")
            try:
                # No direct checkpoint exists: discard only this job's incomplete extraction.
                if os.path.isdir(staging):
                    shutil.rmtree(staging)
                adoption = zipsafe.adopt_result_zip(zip_path, staging)
                primary = zipsafe.select_primary_markdown(adoption["members"])
            except zipsafe.ZipAdoptionError as exc:
                return _blocked("conversion_result_invalid", str(exc))
            direct_path = os.path.join(staging, *primary["path"].split("/"))
            with open(direct_path, "rb") as fh:
                direct_bytes = fh.read()
            # 采用期资产本地化归一（§10.4）：必须在冻结之前。冻结之后再动
            # 字节就是违反「直接稿逐字节不可变」，而不动就等于把「直接稿引用
            # 外链」冻进合同——那正是编号 27 无法恢复的成因。
            members = list(adoption["members"])
            outcome = localize.localize_markdown(
                direct_bytes, os.path.dirname(direct_path),
                [{**m, "path": os.path.relpath(os.path.join(staging, m["path"]), os.path.dirname(direct_path))} for m in members],
                self.services
            )
            self._write_localization_trace(outcome)
            if outcome["kind"] != localize.UNCHANGED:
                # 失败后必须清空暂存树：本阶段没有设置 direct phase，重跑会
                # 重新逐成员 O_EXCL 解压，留着旧树会撞成 FileExistsError 并
                # 逃出 ZipAdoptionError 的分类（同 start_paid_successor_attempt）。
                if outcome["kind"] != localize.LOCALIZED:
                    shutil.rmtree(staging, ignore_errors=True)
                if outcome["kind"] == localize.ENVIRONMENT_UNAVAILABLE:
                    return _blocked(
                        "network_environment_unavailable", outcome["summary"]
                    )
                if outcome["kind"] == localize.INVALID:
                    return _blocked(
                        "conversion_result_invalid", outcome["summary"],
                        successor_eligible=False,
                    )
                if outcome["kind"] == localize.UNAVAILABLE:
                    return _blocked("conversion_result_invalid", outcome["summary"])
                direct_bytes = outcome["markdown"]
                fsatomic.atomic_replace_bytes(
                    direct_path, direct_bytes, "localized direct markdown"
                )
                digest = hashlib.sha256(direct_bytes).hexdigest()
                primary = dict(
                    primary, sha256=digest, byte_count=len(direct_bytes)
                )
                members = [
                    dict(m, sha256=digest, byte_count=len(direct_bytes))
                    if m["path"] == primary["path"] else m
                    for m in members
                ] + [{**m, "path": os.path.relpath(os.path.join(os.path.dirname(direct_path), m["path"]), staging)} for m in outcome["members"]]
            try:
                blocks = mdinventory.parse_blocks(
                    direct_bytes, self.services["pandoc_path"]
                )
            except mdinventory.MdInventoryError as exc:
                return _blocked("conversion_result_invalid", str(exc))
            # 票 08：源结构清单 vs 直转稿实际引用的图片，差集如实留证。
            omitted_images = []
            source = self._phase("source")
            if source and source.get("inventory_path"):
                try:
                    with open(source["inventory_path"], "rb") as fh:
                        inventory = jcs.loads_strict(fh.read())
                    omitted_images = review.reconcile_omitted_images(
                        inventory, blocks, direct_bytes
                    )
                except (OSError, ValueError):
                    omitted_images = []
            self._set_phase(
                "direct",
                {
                    "staging": staging,
                    "primary_path": direct_path,
                    "sha256": primary["sha256"],
                    "byte_count": primary["byte_count"],
                    "members": members,
                    "md_blocks": blocks,
                    "omitted_images": omitted_images,
                },
            )
        return CoreResult("artifacts_prepared", direct=self._phase("direct"))

    # ---- review（宿主判断 action） ----

    def _review_units(self):
        """（首轮）核验单元全集：始终基于原始直转稿构造，块宇宙冻结（§10.5）。"""
        source = self._phase("source")
        direct = self._phase("direct")
        with open(direct["primary_path"], "rb") as fh:
            direct_bytes = fh.read()
        assignment = review.assign_blocks_to_pages(
            direct["md_blocks"], source["page_count"], direct_bytes
        )
        return review.build_review_units(
            source["pages"], direct["md_blocks"], direct_bytes, assignment
        ), direct_bytes, assignment

    def review_requests(self):
        """未完成核验的单元请求（workflow 逐个包装为 host intent）。

        核验期间**不改稿**（票 05）：本方法只发请求；已核验稿在全部单元
        （含复核轮，票 06）完成后由 `_finalize_review` 一次性产出。
        """
        source = self._phase("source")
        direct = self._phase("direct")
        if source is None or direct is None:
            return _failed("action_contract_invalid", "phases missing for review")
        units, direct_bytes, assignment = self._review_units()
        phase = self._phase("review") or _empty_review_phase()
        done = set(phase["completed_units"])
        pending = [u for u in units if u["review_unit_id"] not in done]
        if pending:
            return CoreResult(
                "host_action_requested", request=pending[0], all_units=len(units)
            )
        # 首轮已全部完成 → 复核轮（票 06）：只复核出现过替换的页。
        try:
            recheck = review.build_recheck_units(
                units, phase, source["pages"], direct["md_blocks"], direct_bytes
            )
        except review.ReviewBudgetExceeded as exc:
            return _blocked("state_conflict", str(exc))
        pending_recheck = [
            u for u in recheck if u["review_unit_id"] not in done
        ]
        if pending_recheck:
            return CoreResult(
                "host_action_requested",
                request=pending_recheck[0],
                all_units=len(units) + len(recheck),
            )
        return self._finalize_review(direct_bytes)

    def record_review_response(self, unit, reviews, page_findings=None):
        """覆盖校验 + **累积**裁决；不写文档（票 05）。"""
        phase = self._phase("review") or _empty_review_phase()
        budget = review.ReviewBudget.from_state(phase.get("budget"))
        # 该页已核验过一轮 → 本次是复核轮，裁决对象是替换后的文本（票 06）。
        is_recheck = budget.rounds_for(unit["page_number"]) >= 1
        try:
            review.check_coverage(unit, reviews)
            budget.charge(unit["page_number"])
        except review.ReviewIncomplete as exc:
            return _blocked("review_incomplete", str(exc))
        except review.ReviewBudgetExceeded as exc:
            return _blocked("state_conflict", str(exc))
        accumulator = review.ReviewAccumulator.from_state(phase.get("accumulated"))
        accumulator.absorb(reviews, recheck=is_recheck)
        phase["completed_units"].append(unit["review_unit_id"])
        phase.setdefault("page_findings", {})[unit["review_unit_id"]] = list(page_findings or [])
        phase.setdefault("responses", []).append({"unit_id": unit["review_unit_id"],
            "page_number": unit["page_number"], "round": 2 if is_recheck else 1,
            "reviews": reviews, "page_findings": list(page_findings or [])})
        if is_recheck:
            # The two-round budget cannot certify text changed in the final round.
            phase.setdefault("unverified_replacements", []).extend(
                item["block_id"] for item in reviews if item["verdict"] in review.REPLACEMENT_VERDICTS)
        phase["accumulated"] = accumulator.as_state()
        phase["budget"] = budget.as_state()
        self._set_phase("review", phase)
        return CoreResult("artifacts_prepared", review=phase)

    def _finalize_review(self, direct_bytes):
        """全部单元完成 → 对**原始**直转稿一次性应用所有裁决（票 05）。

        每个块至多贡献一条 edit（累积器按 block_id 覆盖），且所有 span 都取自
        同一份原始直转稿，故编辑区间天然互不重叠——不存在需要平移的中间态。
        """
        direct = self._phase("direct")
        phase = self._phase("review") or _empty_review_phase()
        if phase.get("verified_path"):
            return CoreResult("artifacts_prepared", review=phase)
        accumulator = review.ReviewAccumulator.from_state(phase.get("accumulated"))
        try:
            revised, meta = review.apply_reviews(
                direct_bytes, direct["md_blocks"], accumulator.reviews()
            )
        except review.ReviewIncomplete as exc:
            return _blocked("review_incomplete", str(exc))
        verified_path = os.path.join(self.work_dir, "04-review", "verified.md")
        fsatomic.ensure_private_dir(os.path.dirname(verified_path), "review dir")
        fsatomic.atomic_replace_bytes(verified_path, revised, "verified markdown")
        phase["verified_path"] = verified_path
        phase["verified_sha256"] = hashlib.sha256(revised).hexdigest()
        phase["lineage"] = meta["lineage"]
        # 复核轮判 extra/unreadable 但保留了首轮替换的块，其标记不在 verdict 里，
        # 需要单独并入（票 06 的语义拍板：文本用替换、标记照记）。
        flags = {key: list(meta["flags"][key]) for key in ("extra", "unreadable")}
        for key, block_ids in accumulator.recheck_flags().items():
            for block_id in block_ids:
                if block_id not in flags[key]:
                    flags[key].append(block_id)
        phase["flags"] = {key: sorted(value) for key, value in flags.items()}
        self._set_phase("review", phase)
        return CoreResult("artifacts_prepared", review=phase)
