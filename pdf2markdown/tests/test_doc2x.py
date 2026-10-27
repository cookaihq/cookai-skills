"""基线 §10.3 / §19.1：Doc2X 请求构造、响应校验、轮询窗口预算。"""

import json
import unittest

from pdf2markdown import doc2x


def _body(**over):
    body = {
        "id": "task-1",
        "model": "doc2x-v3",
        "type": "document",
        "status": "processing",
    }
    body.update(over)
    return json.dumps(body).encode("utf-8")


class ResultPendingTest(unittest.TestCase):
    def test_completed_with_empty_results_stays_pending(self):
        # live 定型：completed 初期 results 短暂为空 → 继续轮询，不判失败。
        for raw in (_body(status="completed", results=None),
                    _body(status="completed", results=[]),
                    _body(status="completed")):
            out = doc2x.validate_task_response(200, raw, "task-1")
            self.assertEqual(out, {"status": "processing", "result_url": None})


class RequestBuildTest(unittest.TestCase):
    def test_origin_validation(self):
        doc2x.validate_origin("https://api.example.com")
        doc2x.validate_origin("https://api.example.com:8443/")
        for bad in (
            "http://api.example.com",
            "https://u:p@api.example.com",
            "https://api.example.com/v1",
            "https://api.example.com?x=1",
        ):
            with self.assertRaises(doc2x.Doc2XReason):
                doc2x.validate_origin(bad)

    def test_create_request_exact_shape(self):
        req = doc2x.build_create_request(
            "https://api.example.com/", "https://oss.example.com/src.pdf", 3, "a" * 64, True
        )
        self.assertEqual(req["url"], "https://api.example.com/v1/run/generations")
        self.assertEqual(
            set(req["body"].keys()),
            {
                "model",
                "pdf_url",
                "page_count",
                "convert_mode",
                "formula_mode",
                "filename",
                "merge_cross_page_forms",
            },
        )
        self.assertEqual(req["body"]["model"], "doc2x-v3")
        self.assertEqual(req["body"]["convert_mode"], "md")
        self.assertEqual(req["body"]["formula_mode"], "dollar")
        self.assertTrue(req["body"]["merge_cross_page_forms"])

    def test_task_request_percent_encoding(self):
        req = doc2x.build_task_request("https://api.example.com", "task/with?chars")
        self.assertIn("/v1/tasks/task%2Fwith%3Fchars?sync_upstream=true", req["url"])


class CreateResponseTest(unittest.TestCase):
    def test_accepts_valid(self):
        out = doc2x.validate_create_response(200, _body(status="pending"))
        self.assertEqual(out, {"task_id": "task-1", "status": "pending"})

    def test_2xx_bad_body_is_submission_unknown(self):
        for raw in (b"not json", _body(model="other"), _body(status="odd"), b"{}"):
            with self.assertRaises(doc2x.Doc2XReason) as ctx:
                doc2x.validate_create_response(200, raw)
            self.assertEqual(ctx.exception.raw_reason, "submission_unknown")

    def test_http_classification(self):
        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            doc2x.validate_create_response(429, b"")
        self.assertEqual(ctx.exception.raw_reason, "submission_unknown")
        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            doc2x.validate_create_response(422, b"")
        self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")


class TaskResponseTest(unittest.TestCase):
    def test_completed_single_result(self):
        raw = _body(status="completed", results=[{"url": "https://r.example.com/z.zip"}])
        out = doc2x.validate_task_response(200, raw, "task-1")
        self.assertEqual(out["result_url"], "https://r.example.com/z.zip")

    def test_completed_shape_violations(self):
        cases = [
            _body(status="completed", results=[{"url": "https://a"}, {"url": "https://b"}, {"url": "https://c"}]),
            _body(status="completed", results=[{"url": "https://a"}, {"url": "https://b"}]),
            _body(status="completed", results=[{"url": "http://insecure"}]),
            _body(status="completed", results=[{"url": "https://a", "extra": 1}]),
        ]
        for raw in cases:
            with self.assertRaises(doc2x.Doc2XReason) as ctx:
                doc2x.validate_task_response(200, raw, "task-1")
            self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")

    def test_failed_maps_to_task_failed(self):
        raw = _body(status="failed", error={"type": "e"})
        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            doc2x.validate_task_response(200, raw, "task-1")
        self.assertEqual(ctx.exception.raw_reason, "doc2x_task_failed")

    def test_task_identity_mismatch(self):
        with self.assertRaises(doc2x.Doc2XReason):
            doc2x.validate_task_response(200, _body(id="task-2"), "task-1")

    def test_429_and_5xx_are_transient_not_fatal(self):
        """限流与服务端错误不说明 task 坏了：窗口继续，不作废已付费任务。"""
        for status in (429, 500, 502, 503, 504, 599):
            with self.assertRaises(doc2x.Doc2XPollTransient) as ctx:
                doc2x.validate_task_response(status, b"", "task-1")
            self.assertEqual(ctx.exception.category, "http_%d" % status)
            self.assertNotIsInstance(ctx.exception, doc2x.Doc2XReason)

    def test_auth_and_missing_and_other_4xx_stay_fatal(self):
        for status in (400, 401, 403, 404, 418):
            with self.assertRaises(doc2x.Doc2XReason) as ctx:
                doc2x.validate_task_response(status, b"", "task-1")
            self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")

    def test_2xx_bad_body_stays_fatal(self):
        for raw in (b"not json", _body(model="other"), _body(status="odd")):
            with self.assertRaises(doc2x.Doc2XReason) as ctx:
                doc2x.validate_task_response(200, raw, "task-1")
            self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")


class _ClockHarness(object):
    """假单调时钟 + 假 sleep：sleep 直接推进时钟，测试不真的等待。"""

    def _window(self, trace=None):
        self.clock = [0.0]
        self.slept = []

        def monotonic():
            return self.clock[0]

        def sleep(seconds):
            self.slept.append(seconds)
            self.clock[0] += seconds

        return doc2x.PollWindow(monotonic=monotonic, sleep=sleep, trace=trace)


class PollWindowTest(_ClockHarness, unittest.TestCase):
    def test_completes_within_window(self):
        window = self._window()
        responses = [
            (200, _body(status="processing")),
            (200, _body(status="completed", results=[{"url": "https://r/z.zip"}])),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            self.clock[0] += 1.0
            return responses[len(calls) - 1]

        out = window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(len(calls), 2)
        # 相邻请求开始至少间隔 5 秒
        self.assertTrue(all(s >= 0 for s in self.slept))
        self.assertGreaterEqual(sum(self.slept), 4.0)

    def test_get_budget_exhausted(self):
        window = self._window()

        def transport(request):
            self.clock[0] += 0.001
            return (200, _body(status="processing"))

        # 拉长窗口时钟不动：只测次数上限——用小间隔推进
        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            # 手动把时间控制在 600s 内完成 120 次
            original_sleep = self.slept
            window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(ctx.exception.raw_reason, "doc2x_poll_budget_exhausted")
        self.assertLessEqual(window.gets_used, doc2x.POLL_MAX_GETS)

    def test_wall_clock_exhausted(self):
        window = self._window()

        def transport(request):
            self.clock[0] += 100.0  # 每次 GET 消耗 100s
            return (200, _body(status="processing"))

        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(ctx.exception.raw_reason, "doc2x_poll_budget_exhausted")
        self.assertLess(window.gets_used, 10)


class PollWindowTransientToleranceTest(_ClockHarness, unittest.TestCase):
    """事故回归：单次轮询失败曾判死整个已付费 task（record 1 的真实形态）。"""

    COMPLETED = _body(status="completed", results=[{"url": "https://r/z.zip"}])

    def _harvest_after(self, first_failure):
        """第一次 GET 失败、第二次成功采收 completed。"""
        window = self._window()
        calls = []

        def transport(request):
            calls.append(request)
            self.clock[0] += 1.0
            if len(calls) == 1:
                if isinstance(first_failure, Exception):
                    raise first_failure
                return (first_failure, b"")
            return (200, self.COMPLETED)

        out = window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(out["status"], "completed")
        self.assertEqual(out["result_url"], "https://r/z.zip")
        # 失败那次照常消耗 GET 预算，且被计入失败观察
        self.assertEqual(window.gets_used, 2)
        self.assertEqual(window.transient_failures, 1)
        # 两次 GET 之间仍遵守 5 秒最小间隔（失败不豁免节流）
        self.assertGreaterEqual(sum(self.slept), 4.0)

    def test_transport_failure_then_success(self):
        self._harvest_after(doc2x.Doc2XPollTransient("transport", "TimeoutError"))

    def test_http_429_then_success(self):
        self._harvest_after(429)

    def test_http_500_then_success(self):
        self._harvest_after(500)

    def test_http_503_then_success(self):
        self._harvest_after(503)

    def test_continuous_transient_exhausts_get_budget(self):
        from unittest.mock import patch
        window = self._window()

        def transport(request):
            self.clock[0] += 0.001
            raise doc2x.Doc2XPollTransient("transport", "TimeoutError")

        # Exercise the count limit independently of backoff's wall-clock limit.
        with patch.object(doc2x, "POLL_MAX_GETS", 3), self.assertRaises(doc2x.Doc2XReason) as ctx:
            window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(ctx.exception.raw_reason, "doc2x_poll_budget_exhausted")
        self.assertEqual(ctx.exception.masked_summary, "poll window GET budget exhausted")
        self.assertEqual(window.gets_used, 3)
        self.assertEqual(window.transient_failures, 3)

    def test_continuous_transient_exhausts_wall_clock(self):
        window = self._window()

        def transport(request):
            self.clock[0] += 100.0
            return (500, b"")

        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(ctx.exception.raw_reason, "doc2x_poll_budget_exhausted")
        self.assertEqual(ctx.exception.masked_summary, "poll window wall clock exhausted")
        self.assertLess(window.gets_used, doc2x.POLL_MAX_GETS)
        self.assertEqual(window.transient_failures, window.gets_used)

    def test_transient_never_escapes_poll(self):
        """瞬时异常穿透出去就会被 run_doc2x 当成未知异常，作废整轮转换。"""
        window = self._window()

        def transport(request):
            self.clock[0] += 100.0
            raise doc2x.Doc2XPollTransient("transport", "ConnectionResetError")

        try:
            window.poll(transport, "https://api.example.com", "task-1")
        except doc2x.Doc2XPollTransient:
            self.fail("Doc2XPollTransient escaped PollWindow.poll")
        except doc2x.Doc2XReason as exc:
            self.assertEqual(exc.raw_reason, "doc2x_poll_budget_exhausted")

    def test_fatal_status_still_stops_immediately(self):
        for status in (401, 404, 400):
            window = self._window()
            calls = []

            def transport(request, status=status, calls=calls):
                calls.append(request)
                self.clock[0] += 1.0
                return (status, b"")

            with self.assertRaises(doc2x.Doc2XReason) as ctx:
                window.poll(transport, "https://api.example.com", "task-1")
            self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")
            self.assertEqual(len(calls), 1)

    def test_2xx_bad_body_still_stops_immediately(self):
        window = self._window()
        calls = []

        def transport(request):
            calls.append(request)
            self.clock[0] += 1.0
            return (200, b"not json")

        with self.assertRaises(doc2x.Doc2XReason) as ctx:
            window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(ctx.exception.raw_reason, "conversion_result_invalid")
        self.assertEqual(len(calls), 1)

    def test_trace_records_every_get(self):
        records = []
        window = self._window(trace=records.append)
        responses = [
            doc2x.Doc2XPollTransient("transport", "TimeoutError"),
            (500, b""),
            (200, _body(status="processing")),
            (200, self.COMPLETED),
        ]
        calls = []

        def transport(request):
            calls.append(request)
            self.clock[0] += 1.0
            item = responses[len(calls) - 1]
            if isinstance(item, Exception):
                raise item
            return item

        window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(
            [r["outcome"] for r in records],
            ["transient:transport", "transient:http_500", "processing", "completed"],
        )
        self.assertEqual([r["get_index"] for r in records], [1, 2, 3, 4])
        self.assertIn("TimeoutError", records[0]["detail"])
        self.assertIsNone(records[3]["detail"])
        # 单调偏移相对首次 GET 递增
        offsets = [r["monotonic_offset_s"] for r in records]
        self.assertEqual(offsets, sorted(offsets))

    def test_trace_callback_failure_does_not_break_polling(self):
        def broken(record):
            raise RuntimeError("disk full")

        window = self._window(trace=broken)

        def transport(request):
            self.clock[0] += 1.0
            return (200, self.COMPLETED)

        out = window.poll(transport, "https://api.example.com", "task-1")
        self.assertEqual(out["status"], "completed")


if __name__ == "__main__":
    unittest.main()
