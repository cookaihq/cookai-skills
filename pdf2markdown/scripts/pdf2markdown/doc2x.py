"""Doc2X 客户端（基线 §10.3、§16.2）。

- 唯一创建 endpoint：`POST <origin>/v1/run/generations`（JSON、Bearer）。
- 唯一查询 endpoint：`GET <origin>/v1/tasks/{percent-encoded-id}?sync_upstream=true`。
- 每个 conversion attempt 最多一次 POST；合法 task ID 后只轮询同 task。
- 轮询窗口：最多 120 次 GET、相邻请求开始至少间隔 5 秒、单调时钟总时长
  最多 600 秒；上限先到且仍 pending/processing → `doc2x_poll_budget_exhausted`。
- 单次 GET 的传输失败 / HTTP 429 / HTTP 5xx 是**瞬时失败**：消耗当次 GET
  预算后窗口继续下一次计划内 GET，不作废已付费的 task。
- transport 可注入：fake 测试不发真实网络；真实 transport 遵守 §16.2
  deadline（connect 10s；create read 60s；task GET read 30s）且不跟随
  redirect、不自动 retry。
"""

import json
import re
import time
import urllib.parse

from . import strictschema

CREATE_PATH = "/v1/run/generations"
TASK_PATH_TEMPLATE = "/v1/tasks/%s?sync_upstream=true"

MODEL = "doc2x-v3"
CONVERT_MODE = "md"
FORMULA_MODE = "dollar"

POLL_MAX_GETS = 120
POLL_MIN_INTERVAL_SECONDS = 5
POLL_WINDOW_SECONDS = 600

TASK_STATUSES = ("pending", "processing", "completed", "failed")

_ORIGIN_RE = re.compile(r"^https://[A-Za-z0-9.-]+(:\d+)?/?$")


class Doc2XReason(Exception):
    """携带 conversion-core raw reason 的确定停止。"""

    def __init__(self, raw_reason, masked_summary):
        super().__init__(masked_summary)
        self.raw_reason = raw_reason
        self.masked_summary = masked_summary


class Doc2XPollTransient(Exception):
    """轮询窗口内可容忍的单次失败——**不是**确定停止，故不复用 Doc2XReason。

    上游任务已经付过费且可能几十秒内就完成；一次 read timeout 或 429/5xx
    只说明「这一次没问过来」，不说明任务坏了。它只能在 `PollWindow.poll`
    内部被消化（消耗当次 GET 预算后继续窗口），绝不允许逃逸到 poll() 之外。

    category：`transport`（传输层失败）或 `http_<状态码>`。
    detail：掩码摘要级别的短文本（含异常类型名），只用于本地痕迹记录。
    """

    def __init__(self, category, detail=""):
        super().__init__(detail or category)
        self.category = category
        self.detail = detail


def validate_origin(base_url):
    """base URL 只能是 HTTPS origin：无 userinfo/query/fragment，path 空或 /。"""
    if not isinstance(base_url, str) or not _ORIGIN_RE.match(base_url):
        raise Doc2XReason(
            "credential_drift",
            "doc2x base url is not a bare https origin",
        )
    return base_url.rstrip("/")


def build_create_request(base_url, pdf_url, page_count, source_sha256,
                         merge_cross_page_forms):
    """构造创建请求（exact keys；§10.3）。"""
    origin = validate_origin(base_url)
    body = {
        "model": MODEL,
        "pdf_url": pdf_url,
        "page_count": page_count,
        "convert_mode": CONVERT_MODE,
        "formula_mode": FORMULA_MODE,
        "filename": "source-%s.pdf" % source_sha256[:16],
        "merge_cross_page_forms": bool(merge_cross_page_forms),
    }
    return {
        "method": "POST",
        "url": origin + CREATE_PATH,
        "body": body,
    }


def build_task_request(base_url, task_id):
    origin = validate_origin(base_url)
    if not strictschema.is_opaque_id(task_id):
        raise Doc2XReason("conversion_result_invalid", "task id is not an opaque id")
    return {
        "method": "GET",
        "url": origin + TASK_PATH_TEMPLATE % urllib.parse.quote(task_id, safe=""),
        "body": None,
    }


def _parse_json(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def validate_create_response(status_code, raw_body):
    """返回 task 描述 dict；无法证明接受与否 → submission_unknown。

    2xx JSON 至少验证非空 id、model、type=document、四态 status；
    HTTP 400/401/402/422/429/500/503 只按 status 分类。
    """
    if 200 <= status_code < 300:
        body = _parse_json(raw_body)
        if body is None:
            raise Doc2XReason("submission_unknown", "create returned 2xx without valid JSON")
        task_id = body.get("id")
        if (
            not strictschema.is_opaque_id(task_id)
            or body.get("model") != MODEL
            or body.get("type") != "document"
            or body.get("status") not in TASK_STATUSES
        ):
            raise Doc2XReason(
                "submission_unknown", "create 2xx body failed field verification"
            )
        return {"task_id": task_id, "status": body["status"]}
    if status_code in (400, 401, 402, 422):
        # provider 确定拒绝：POST 未被接受 → 不消耗成功创建，按结果无效停止。
        raise Doc2XReason(
            "conversion_result_invalid",
            "doc2x create rejected with http %d" % status_code,
        )
    if status_code in (429, 500, 503):
        raise Doc2XReason(
            "submission_unknown",
            "doc2x create returned http %d; acceptance unprovable" % status_code,
        )
    raise Doc2XReason(
        "submission_unknown", "doc2x create returned unregistered http %d" % status_code
    )


def validate_task_response(status_code, raw_body, expected_task_id):
    """返回 {status, result_url|None}；瞬时失败抛 Doc2XPollTransient，其余按登记 reason 停止。"""
    if 200 <= status_code < 300:
        body = _parse_json(raw_body)
        if body is None:
            raise Doc2XReason("conversion_result_invalid", "task 2xx without valid JSON")
        if (
            body.get("id") != expected_task_id
            or body.get("model") != MODEL
            or body.get("type") != "document"
            or body.get("status") not in TASK_STATUSES
        ):
            raise Doc2XReason(
                "conversion_result_invalid", "task body failed field verification"
            )
        status = body["status"]
        if status == "completed":
            result = body.get("results")  # 真实 provider 字段为复数（live 定型）
            if result is None or result == []:
                # provider 在 completed 初期短暂返回空 results（result-pending，
                # live 定型）：按仍在进行处理，留在轮询窗口内继续查询。
                return {"status": "processing", "result_url": None}
            if not isinstance(result, list) or len(result) != 1:
                raise Doc2XReason(
                    "conversion_result_invalid",
                    "completed task must carry exactly one result",
                )
            item = result[0]
            if not isinstance(item, dict) or set(item.keys()) != {"url"}:
                raise Doc2XReason(
                    "conversion_result_invalid", "result element exact keys must be {url}"
                )
            url = item["url"]
            if not isinstance(url, str) or not url.startswith("https://"):
                raise Doc2XReason(
                    "conversion_result_invalid", "result url failed download policy"
                )
            return {"status": status, "result_url": url}
        if status == "failed":
            error = body.get("error")
            if not isinstance(error, dict):
                raise Doc2XReason(
                    "conversion_result_invalid", "failed task lacks structured error"
                )
            raise Doc2XReason("doc2x_task_failed", "doc2x task reported failure")
        return {"status": status, "result_url": None}
    if status_code == 429 or 500 <= status_code < 600:
        # 限流与服务端错误都不说明 task 本身有问题：留在窗口内继续查。
        raise Doc2XPollTransient(
            "http_%d" % status_code,
            "doc2x task query returned http %d" % status_code,
        )
    if status_code in (401, 404):
        raise Doc2XReason(
            "conversion_result_invalid",
            "doc2x task query returned http %d" % status_code,
        )
    raise Doc2XReason(
        "conversion_result_invalid",
        "doc2x task query returned unregistered http %d" % status_code,
    )


class PollWindow:
    """一个授权轮询窗口：120 次 / 5s 间隔 / 单调 600s（§10.3）。"""

    def __init__(self, monotonic=None, sleep=None, trace=None):
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._trace = trace
        self.gets_used = 0
        self.transient_failures = 0
        self._first_get_at = None
        self._last_get_at = None
        self._retry_delay = 0

    def _record(self, outcome, detail):
        """每次 GET 后追加一条痕迹；回调自身异常不得影响轮询（纯诊断）。"""
        if self._trace is None:
            return
        try:
            self._trace(
                {
                    "get_index": self.gets_used,
                    "monotonic_offset_s": round(
                        self._monotonic() - self._first_get_at, 3
                    ),
                    "outcome": outcome,
                    "detail": detail,
                }
            )
        except Exception:
            pass

    def poll(self, transport, base_url, task_id):
        """在窗口内轮询直到 completed/failed 或预算耗尽。

        transport(request) -> (status_code, raw_body)。返回 completed 的
        {status, result_url}；预算耗尽抛 doc2x_poll_budget_exhausted。

        单次 GET 的传输失败、HTTP 429 与 5xx 记为一次失败观察：消耗当次 GET
        预算，等到下一个间隔后继续计划内 GET。Doc2XPollTransient 不得穿透本
        方法——否则一次瞬时失败会作废一个已付费且可能已完成的 task。
        """
        while True:
            now = self._monotonic()
            if self._first_get_at is not None:
                if self.gets_used >= POLL_MAX_GETS:
                    raise Doc2XReason(
                        "doc2x_poll_budget_exhausted", "poll window GET budget exhausted"
                    )
                if now - self._first_get_at >= POLL_WINDOW_SECONDS:
                    raise Doc2XReason(
                        "doc2x_poll_budget_exhausted", "poll window wall clock exhausted"
                    )
                wait = max(POLL_MIN_INTERVAL_SECONDS, self._retry_delay) - (now - self._last_get_at)
                if wait > 0:
                    self._sleep(min(wait, POLL_WINDOW_SECONDS - (now - self._first_get_at)))
                    now = self._monotonic()
                    if now - self._first_get_at >= POLL_WINDOW_SECONDS:
                        raise Doc2XReason(
                            "doc2x_poll_budget_exhausted",
                            "poll window wall clock exhausted",
                        )
            request = build_task_request(base_url, task_id)
            start = self._monotonic()
            if self._first_get_at is None:
                self._first_get_at = start
            self._last_get_at = start
            self.gets_used += 1
            try:
                request["timeout"] = max(0.01, POLL_WINDOW_SECONDS - (self._monotonic() - self._first_get_at))
                status_code, raw_body = transport(request)
                outcome = validate_task_response(status_code, raw_body, task_id)
            except Doc2XPollTransient as exc:
                self.transient_failures += 1
                self._retry_delay = getattr(exc, "retry_after", None) or min(60, 2 ** min(self.transient_failures - 1, 6))
                import sys
                sys.stderr.write("[pdf2markdown] task query %s/%s, retry in %ss (%s)\n" %
                                 (self.gets_used, POLL_MAX_GETS, max(POLL_MIN_INTERVAL_SECONDS, self._retry_delay), exc.category))
                self._record("transient:%s" % exc.category, exc.detail)
                continue
            except Doc2XReason as exc:
                self._record(
                    "failed" if exc.raw_reason == "doc2x_task_failed"
                    else "fatal:%s" % exc.raw_reason,
                    exc.raw_reason,
                )
                raise
            self._retry_delay = 0
            self._record(outcome["status"], None)
            if outcome["status"] == "completed":
                return outcome
            # pending/processing：继续窗口内轮询。
