import importlib.util
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "image_task.py"
SPEC = importlib.util.spec_from_file_location("image_2_runtime", SCRIPT_PATH)
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contracts" / "validate.py"
CONTRACT_SPEC = importlib.util.spec_from_file_location("image_2_contract", CONTRACT_PATH)
contract = importlib.util.module_from_spec(CONTRACT_SPEC)
sys.modules[CONTRACT_SPEC.name] = contract
CONTRACT_SPEC.loader.exec_module(contract)


class FakeApiTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, method, url, headers, body=None):
        self.requests.append((method, url, dict(headers), body))
        action = self.responses.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action


class FakeDownloadTransport:
    def __init__(self, body=b"png-bytes"):
        self.body = body
        self.requests = []

    def fetch(self, request, sink):
        self.requests.append(request)
        sink.write(self.body)
        return runtime.OneHopResponse(200, ())


class RoutedDownloadTransport:
    def __init__(self, routes):
        self.routes = {url: list(actions) for url, actions in routes.items()}
        self.requests = []

    def fetch(self, request, sink):
        self.requests.append(request)
        action = self.routes[request.url].pop(0)
        if isinstance(action, BaseException):
            raise action
        if callable(action):
            return action(request, sink)
        status, headers, body = action
        if body:
            sink.write(body)
        return runtime.OneHopResponse(status, tuple(headers))


class JsonRuntimeTests(unittest.TestCase):
    def invoke(self, responses, *extra_args, download=None, max_attempts="1"):
        api = FakeApiTransport(responses)
        download = FakeDownloadTransport() if download is None else download
        stdout = io.StringIO()
        stderr = io.StringIO()
        sleeps = []
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as output:
            exit_code = runtime.main(
                [
                    "--json",
                    "--prompt",
                    "cover",
                    "--output-dir",
                    output,
                    "--poll-interval",
                    "1",
                    "--max-attempts",
                    max_attempts,
                    *extra_args,
                ],
                api_transport=api,
                download_transport=download,
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=sleeps.append,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
            document = json.loads(stdout.getvalue())
            contract_errors = contract.validate_document(
                document,
                ["test-runtime-primary-key"],
            )
            if contract_errors:
                self.fail(f"runtime result violated Ticket 34: {contract_errors}")
        return exit_code, document, stderr.getvalue(), api, download, sleeps

    def test_json_success_is_one_document_with_an_atomic_local_result(self):
        key = "test-runtime-primary-key"
        create = runtime.HttpResponse(
            200,
            (),
            b'{"id":"task-runtime-1","status":"pending"}',
        )
        completed = runtime.HttpResponse(
            200,
            (),
            b'{"status":"completed","results":[{"url":"https://images.example.test/one.png?signature=independent","content_type":"image/png"}]}',
        )
        api = FakeApiTransport([create, completed])
        download = FakeDownloadTransport()

        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as output:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                [
                    "--json",
                    "--prompt",
                    "cover",
                    "--output-dir",
                    output,
                    "--filename",
                    "cover",
                    "--poll-interval",
                    "1",
                    "--max-attempts",
                    "1",
                ],
                api_transport=api,
                download_transport=download,
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                clock=lambda: datetime(2026, 7, 22, tzinfo=timezone.utc),
                environ={"AIHUB_API_KEY": key},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )

            self.assertEqual(exit_code, 0)
            document = json.loads(stdout.getvalue())
            self.assertEqual(
                set(document),
                {"schema_version", "task_id", "status", "outputs", "error"},
            )
            self.assertEqual(document["status"], "ok")
            self.assertEqual(document["outputs"][0]["status"], "saved")
            local_path = Path(document["outputs"][0]["local_path"])
            self.assertTrue(local_path.is_absolute())
            self.assertEqual(local_path.read_bytes(), b"png-bytes")
            self.assertEqual(stdout.getvalue().count("\n"), 1)
            self.assertNotIn(key, stdout.getvalue())
            self.assertNotIn(key, stderr.getvalue())
            self.assertEqual(len(api.requests), 2)
            self.assertEqual(len(download.requests), 1)

    def test_no_save_returns_urls_without_download_calls(self):
        exit_code, document, _, _, download, _ = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-no-save"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/no-save.png","content_type":"image/png"}]}',
                ),
            ],
            "--no-save",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["outputs"][0]["status"], "not_saved")
        self.assertIsNone(document["outputs"][0]["local_path"])
        self.assertEqual(download.requests, [])

    def test_completed_without_results_keeps_polling_until_results_arrive(self):
        exit_code, document, _, api, download, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-completed-race"}'),
                runtime.HttpResponse(200, (), b'{"status":"completed","results":null}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/race.png","content_type":"image/png"}]}',
                ),
            ],
            "--no-save",
            max_attempts="2",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["outputs"][0]["status"], "not_saved")
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(download.requests, [])
        self.assertEqual(sleeps, [1])

    def test_upstream_failure_and_timeout_have_distinct_terminal_exits(self):
        failed_exit, failed, _, _, _, _ = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-upstream-failed"}'),
                runtime.HttpResponse(200, (), b'{"status":"failed","error":{"message":"raw"}}'),
            ]
        )
        self.assertEqual(failed_exit, 2)
        self.assertEqual(failed["task_id"], "task-upstream-failed")
        self.assertEqual(failed["error"]["code"], "upstream_failed")
        self.assertNotIn("raw", json.dumps(failed))

        timeout_exit, timed_out, _, _, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-timeout"}'),
                runtime.HttpResponse(200, (), b'{"status":"pending"}'),
                runtime.HttpResponse(200, (), b'{"status":"processing"}'),
            ],
            max_attempts="2",
        )
        self.assertEqual(timeout_exit, 3)
        self.assertEqual(timed_out["status"], "timed_out")
        self.assertEqual(timed_out["error"]["code"], "poll_timeout")
        self.assertEqual(sleeps, [1])

    def test_preterminal_failures_use_closed_codes_and_exit_one(self):
        cases = [
            (
                [OSError("create network secret body")],
                "create_transport_error",
            ),
            (
                # create 是计费写：5xx 表示服务端收到了请求但没能正常应答，任务
                # 是否已创建并计费无法确认 —— 不重试，落结果不明的终态码。
                [runtime.HttpResponse(503, (), b'raw create body')],
                "create_transport_error",
            ),
            (
                # 确定性 4xx 才是真正的「被拒绝」。
                [runtime.HttpResponse(402, (), b'raw create body')],
                "create_http_error",
            ),
            (
                [runtime.HttpResponse(200, (), b'not-json')],
                "create_response_invalid",
            ),
            (
                [runtime.HttpResponse(200, (), b'{"id":"task-one","id":"task-two"}')],
                "create_response_invalid",
            ),
            (
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-query-transport"}'),
                    OSError("query network secret body"),
                ],
                "query_transport_error",
            ),
            (
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-query-http"}'),
                    # 429 属瞬时：3 次尝试都被限流才落 query_http_error。
                    runtime.HttpResponse(429, (), b'raw query body'),
                    runtime.HttpResponse(429, (), b'raw query body'),
                    runtime.HttpResponse(429, (), b'raw query body'),
                ],
                "query_http_error",
            ),
            (
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-query-invalid"}'),
                    runtime.HttpResponse(200, (), b'{"status":"mystery"}'),
                ],
                "query_response_invalid",
            ),
            (
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-query-duplicate"}'),
                    runtime.HttpResponse(
                        200,
                        (),
                        b'{"status":"pending","status":"completed"}',
                    ),
                ],
                "query_response_invalid",
            ),
        ]
        for responses, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                exit_code, document, _, _, _, _ = self.invoke(responses)
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["status"], "failed")
                self.assertEqual(document["outputs"], [])
                self.assertEqual(document["error"]["code"], expected_code)
                self.assertNotIn("raw", json.dumps(document))
                self.assertNotIn("secret body", json.dumps(document))

    def test_unclassified_local_failure_is_a_closed_internal_error(self):
        api = FakeApiTransport([])
        with tempfile.TemporaryDirectory() as project:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover"],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                clock=lambda: (_ for _ in ()).throw(OSError("local secret detail")),
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 1)
        document = json.loads(stdout.getvalue())
        self.assertEqual(document["error"]["code"], "internal_error")
        self.assertNotIn("local secret detail", stdout.getvalue())
        self.assertEqual(stdout.getvalue().count("\n"), 1)

    def test_invalid_task_ids_are_rejected_before_query_without_echo(self):
        key = "test-runtime-primary-key"
        vectors = [
            "task/id",
            "task?id",
            "task#id",
            "task%2Fid",
            "task\nid",
            "a" * 257,
            key,
        ]
        for task_id in vectors:
            with self.subTest(task_id=repr(task_id[:20])):
                response = json.dumps({"id": task_id}).encode("utf-8")
                exit_code, document, stderr, api, download, _ = self.invoke(
                    [runtime.HttpResponse(200, (), response)]
                )
                self.assertEqual(exit_code, 1)
                self.assertIsNone(document["task_id"])
                self.assertEqual(document["error"]["code"], "invalid_task_id")
                self.assertEqual(len(api.requests), 1)
                self.assertEqual(download.requests, [])
                self.assertNotIn(task_id, json.dumps(document))
                self.assertNotIn(key, stderr)

    def test_invalid_url_or_media_type_rejects_the_whole_query(self):
        vectors = [
            ("http://images.example.test/image.png", "image/png"),
            ("https://user:pass@images.example.test/image.png", "image/png"),
            ("https://images.example.test/image.png#fragment", "image/png"),
            ("https://images.example.test/image%ZZ.png", "image/png"),
            ("https://images.example.test/image.png\nheader", "image/png"),
            ("https://images.example.test/image.png", "image"),
            ("https://images.example.test/image.png", "image/图片"),
            ("https://images.example.test/image.png", "image/test-runtime-primary-key"),
        ]
        for url, content_type in vectors:
            with self.subTest(url=repr(url), content_type=content_type):
                query = json.dumps(
                    {
                        "status": "completed",
                        "results": [
                            {
                                "url": "https://images.example.test/valid.png",
                                "content_type": "image/png",
                            },
                            {"url": url, "content_type": content_type},
                        ],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
                exit_code, document, _, _, download, _ = self.invoke(
                    [
                        runtime.HttpResponse(200, (), b'{"id":"task-invalid-output"}'),
                        runtime.HttpResponse(200, (), query),
                    ]
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["outputs"], [])
                self.assertEqual(document["error"]["code"], "query_response_invalid")
                self.assertEqual(download.requests, [])

    def test_every_loaded_fallback_key_is_checked_for_url_reflection(self):
        primary = "primary-active-key-7f4c"
        fallback = "fallback-active-key-9a2d"
        reflected_urls = [
            f"https://images.example.test/image.png?token={primary}",
            f"https://images.example.test/image.png?token=%70{primary[1:]}",
            f"https://images.example.test/image.png?token={fallback}",
            f"https://images.example.test/image.png?token=%66{fallback[1:]}",
        ]
        for reflected_url in reflected_urls:
            with self.subTest(reflected_url=reflected_url):
                query = json.dumps(
                    {
                        "status": "completed",
                        "results": [{"url": reflected_url, "content_type": "image/png"}],
                    }
                ).encode("utf-8")
                api = FakeApiTransport(
                    [
                        runtime.HttpResponse(401, (), b'{"error":"invalid"}'),
                        runtime.HttpResponse(200, (), b'{"id":"task-fallback"}'),
                        runtime.HttpResponse(200, (), query),
                    ]
                )
                download = FakeDownloadTransport()
                with tempfile.TemporaryDirectory() as project:
                    project_path = Path(project)
                    (project_path / ".env.local").write_text(
                        f"AIHUB_API_KEY={fallback}\n",
                        encoding="utf-8",
                    )
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    exit_code = runtime.main(
                        ["--json", "--prompt", "cover", "--max-attempts", "1"],
                        api_transport=api,
                        download_transport=download,
                        resolver=lambda host, port: ["8.8.8.8"],
                        sleeper=lambda seconds: None,
                        environ={"AIHUB_API_KEY": primary},
                        cwd=project_path,
                        home=project_path / "home",
                        stdout=stdout,
                        stderr=stderr,
                    )
                document = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["error"]["code"], "query_response_invalid")
                self.assertEqual(document["outputs"], [])
                self.assertEqual(len(api.requests), 3)
                self.assertEqual(api.requests[0][2]["Authorization"], f"Bearer {primary}")
                self.assertEqual(api.requests[1][2]["Authorization"], f"Bearer {fallback}")
                self.assertEqual(api.requests[2][2]["Authorization"], f"Bearer {fallback}")
                self.assertEqual(download.requests, [])
                self.assertNotIn(primary, stdout.getvalue())
                self.assertNotIn(fallback, stdout.getvalue())
                self.assertNotIn(primary, stderr.getvalue())
                self.assertNotIn(fallback, stderr.getvalue())

    def test_injected_environment_controls_base_url_and_short_key_is_never_logged(self):
        api = FakeApiTransport(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-custom-base"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/image.png","content_type":"image/png"}]}',
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as project:
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover", "--no-save", "--max-attempts", "1"],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={
                    "AIHUB_API_KEY": "abc",
                    "AIHUBMAX_BASE_URL": "https://custom-api.example.test/base",
                },
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 0)
        self.assertEqual(api.requests[0][1], "https://custom-api.example.test/base/v1/images/generations")
        self.assertNotIn("abc", stdout.getvalue())
        self.assertNotIn("abc", stderr.getvalue())

    def test_existing_generation_options_keep_their_api_payload_contract(self):
        api = FakeApiTransport(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-payload"}'),
                runtime.HttpResponse(200, (), b'{"status":"failed"}'),
            ]
        )
        with tempfile.TemporaryDirectory() as project:
            exit_code = runtime.main(
                [
                    "--json",
                    "--prompt",
                    "redraw",
                    "--resolution",
                    "2400x800",
                    "--num-outputs",
                    "2",
                    "--image-url",
                    "https://refs.example.test/one.png",
                    "--image-url",
                    "https://refs.example.test/two.png",
                    "--quality",
                    "high",
                    "--output-format",
                    "webp",
                    "--background",
                    "opaque",
                    "--max-attempts",
                    "1",
                ],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, 2)
        payload = json.loads(api.requests[0][3])
        self.assertEqual(
            payload,
            {
                "model": "gpt-image-2",
                "prompt": "redraw",
                "num_outputs": 2,
                "resolution": {"width": 2400, "height": 800},
                "quality": "high",
                "output_format": "webp",
                "background": "opaque",
                "image_urls": [
                    "https://refs.example.test/one.png",
                    "https://refs.example.test/two.png",
                ],
            },
        )

    def test_home_key_requires_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as home:
            home_key = "home-active-key-7f4c"
            config_dir = Path(home) / ".config" / "image-2"
            config_dir.mkdir(parents=True)
            (config_dir / ".env").write_text(f"AIHUB_API_KEY={home_key}\n", encoding="utf-8")

            stdout = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover"],
                api_transport=FakeApiTransport([]),
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={},
                cwd=Path(project),
                home=Path(home),
                stdout=stdout,
                stderr=io.StringIO(),
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "configuration_error")

            api = FakeApiTransport(
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-home-key"}'),
                    runtime.HttpResponse(200, (), b'{"status":"failed"}'),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover", "--use-local-key", "--max-attempts", "1"],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={},
                cwd=Path(project),
                home=Path(home),
                stdout=stdout,
                stderr=stderr,
            )
            self.assertEqual(exit_code, 2)
            self.assertEqual(len(api.requests), 2)
            self.assertNotIn(home_key, stdout.getvalue())
            self.assertNotIn(home_key, stderr.getvalue())

    def test_project_key_lookup_does_not_recurse_to_parent(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            (root_path / ".env.local").write_text(
                "AIHUB_API_KEY=parent-key-must-not-load\n",
                encoding="utf-8",
            )
            project = root_path / "child-project"
            project.mkdir()
            api = FakeApiTransport([])
            stdout = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover"],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={},
                cwd=project,
                home=root_path / "home",
                stdout=stdout,
                stderr=io.StringIO(),
            )
        self.assertEqual(exit_code, 1)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "configuration_error")
        self.assertEqual(api.requests, [])

    def test_key_text_in_credential_source_path_is_redacted_from_logs(self):
        key = "path-active-key-7f4c"
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / key / "project"
            project.mkdir(parents=True)
            (project / ".env.local").write_text(f"AIHUB_API_KEY={key}\n", encoding="utf-8")
            api = FakeApiTransport(
                [
                    runtime.HttpResponse(200, (), b'{"id":"task-path-redaction"}'),
                    runtime.HttpResponse(200, (), b'{"status":"failed"}'),
                ]
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                ["--json", "--prompt", "cover", "--max-attempts", "1"],
                api_transport=api,
                download_transport=FakeDownloadTransport(),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={},
                cwd=project,
                home=Path(root) / "home",
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(exit_code, 2)
        self.assertNotIn(key, stdout.getvalue())
        self.assertNotIn(key, stderr.getvalue())

    def test_legacy_x_api_key_still_resolves_and_warns(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir(parents=True)
            (project / ".env.local").write_text("X_API_KEY=sk-legacy-only\n", encoding="utf-8")
            keys = runtime.collect_keys({}, project, Path(root) / "home", False)
        self.assertEqual([k.value for k in keys], ["sk-legacy-only"])
        self.assertTrue(keys[0].legacy)
        self.assertIn("X_API_KEY", keys[0].source)
        self.assertIn("deprecated", keys[0].source)

    def test_canonical_key_wins_over_legacy_within_one_source(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            project.mkdir(parents=True)
            (project / ".env.local").write_text(
                "X_API_KEY=sk-legacy\nAIHUB_API_KEY=sk-canonical\n", encoding="utf-8"
            )
            keys = runtime.collect_keys(
                {"X_API_KEY": "sk-legacy-env", "AIHUB_API_KEY": "sk-canonical-env"},
                project,
                Path(root) / "home",
                False,
            )
        # Source order is preserved (env before .env.local); inside each source the
        # canonical name wins, so neither legacy value appears.
        self.assertEqual([k.value for k in keys], ["sk-canonical-env", "sk-canonical"])
        self.assertFalse(any(k.legacy for k in keys))

    def test_same_and_cross_host_redirects_are_manual_and_keep_original_url(self):
        cases = [
            (
                "https://a.example.test/start.png?signature=independent",
                "/final.png",
                "https://a.example.test/final.png",
            ),
            (
                "https://a.example.test/start.png?signature=independent",
                "https://b.example.test/final.png",
                "https://b.example.test/final.png",
            ),
        ]
        for original, location, final_url in cases:
            with self.subTest(location=location):
                download = RoutedDownloadTransport(
                    {
                        original: [(302, (("Location", location),), b"ignored")],
                        final_url: [(200, (), b"complete-image")],
                    }
                )
                exit_code, document, stderr, _, _, _ = self.invoke(
                    [
                        runtime.HttpResponse(200, (), b'{"id":"task-redirect"}'),
                        runtime.HttpResponse(
                            200,
                            (),
                            json.dumps(
                                {
                                    "status": "completed",
                                    "results": [{"url": original, "content_type": "image/png"}],
                                }
                            ).encode("utf-8"),
                        ),
                    ],
                    download=download,
                )
                self.assertEqual(exit_code, 0)
                self.assertEqual(document["outputs"][0]["upstream_url"], original)
                self.assertEqual([request.url for request in download.requests], [original, final_url])
                for request in download.requests:
                    headers = {key.lower(): value for key, value in request.headers}
                    self.assertNotIn("authorization", headers)
                    self.assertNotIn("x_api_key", headers)
                    self.assertNotIn("cookie", headers)
                    self.assertNotIn("proxy-authorization", headers)
                    self.assertNotIn("referer", headers)
                    self.assertEqual(request.pinned_address, "8.8.8.8")
                self.assertNotIn("independent", stderr)

    def test_redirect_policy_rejects_downgrade_reflection_loop_and_overflow(self):
        key = "test-runtime-primary-key"
        original = "https://a.example.test/start.png"
        policy_cases = [
            (
                "downgrade",
                RoutedDownloadTransport(
                    {original: [(302, (("Location", "http://a.example.test/final.png"),), b"")]}
                ),
                1,
            ),
            (
                "reflected-location",
                RoutedDownloadTransport(
                    {original: [(302, (("Location", f"https://a.example.test/final.png?token={key}"),), b"")]}
                ),
                1,
            ),
            (
                "loop",
                RoutedDownloadTransport(
                    {
                        original: [(302, (("Location", "/next.png"),), b"")],
                        "https://a.example.test/next.png": [
                            (302, (("Location", "/start.png"),), b"")
                        ],
                    }
                ),
                2,
            ),
        ]
        for name, download, expected_requests in policy_cases:
            with self.subTest(name=name):
                exit_code, document, _, _, _, _ = self.invoke(
                    [
                        runtime.HttpResponse(200, (), b'{"id":"task-redirect-policy"}'),
                        runtime.HttpResponse(
                            200,
                            (),
                            json.dumps(
                                {
                                    "status": "completed",
                                    "results": [{"url": original, "content_type": "image/png"}],
                                }
                            ).encode("utf-8"),
                        ),
                    ],
                    download=download,
                )
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["status"], "partial_success")
                self.assertEqual(document["outputs"][0]["status"], "download_failed")
                self.assertIsNone(document["outputs"][0]["local_path"])
                self.assertEqual(len(download.requests), expected_requests)

        chain = [f"https://r.example.test/{index}.png" for index in range(7)]
        routes = {
            chain[index]: [(302, (("Location", chain[index + 1]),), b"")]
            for index in range(6)
        }
        overflow = RoutedDownloadTransport(routes)
        exit_code, document, _, _, _, _ = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-redirect-overflow"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    json.dumps(
                        {
                            "status": "completed",
                            "results": [{"url": chain[0], "content_type": "image/png"}],
                        }
                    ).encode("utf-8"),
                ),
            ],
            download=overflow,
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["outputs"][0]["status"], "download_failed")
        self.assertEqual(len(overflow.requests), 6)

        allowed_chain = [f"https://ok.example.test/{index}.png" for index in range(6)]
        allowed_routes = {
            allowed_chain[index]: [
                (302, (("Location", allowed_chain[index + 1]),), b"")
            ]
            for index in range(5)
        }
        allowed_routes[allowed_chain[5]] = [(200, (), b"five-hop-image")]
        five_hops = RoutedDownloadTransport(allowed_routes)
        exit_code, document, _, _, _, _ = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-five-redirects"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    json.dumps(
                        {
                            "status": "completed",
                            "results": [
                                {"url": allowed_chain[0], "content_type": "image/png"}
                            ],
                        }
                    ).encode("utf-8"),
                ),
            ],
            download=five_hops,
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["outputs"][0]["status"], "saved")
        self.assertEqual(len(five_hops.requests), 6)

    def test_private_or_mixed_dns_is_rejected_before_transport(self):
        original = "https://dns.example.test/image.png"
        for addresses in (["127.0.0.1"], ["8.8.8.8", "10.0.0.1"]):
            with self.subTest(addresses=addresses):
                api = FakeApiTransport(
                    [
                        runtime.HttpResponse(200, (), b'{"id":"task-private-dns"}'),
                        runtime.HttpResponse(
                            200,
                            (),
                            json.dumps(
                                {
                                    "status": "completed",
                                    "results": [{"url": original, "content_type": "image/png"}],
                                }
                            ).encode("utf-8"),
                        ),
                    ]
                )
                download = RoutedDownloadTransport({})
                with tempfile.TemporaryDirectory() as project:
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    exit_code = runtime.main(
                        ["--json", "--prompt", "cover", "--max-attempts", "1"],
                        api_transport=api,
                        download_transport=download,
                        resolver=lambda host, port, result=addresses: result,
                        sleeper=lambda seconds: None,
                        environ={
                            "AIHUB_API_KEY": "test-runtime-primary-key",
                            "HTTPS_PROXY": "http://127.0.0.1:9999",
                            "https_proxy": "http://127.0.0.1:9999",
                        },
                        cwd=Path(project),
                        home=Path(project) / "home",
                        stdout=stdout,
                        stderr=stderr,
                    )
                document = json.loads(stdout.getvalue())
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["outputs"][0]["status"], "download_failed")
                self.assertEqual(download.requests, [])


class DownloadPublicationTests(unittest.TestCase):
    def invoke_download(
        self,
        download,
        output,
        *,
        publisher=runtime.publish_no_replace,
        json_mode=True,
        results=None,
    ):
        if results is None:
            results = [
                {
                    "url": "https://images.example.test/one.png",
                    "content_type": "image/png",
                }
            ]
        api = FakeApiTransport(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-publication"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    json.dumps({"status": "completed", "results": results}).encode("utf-8"),
                ),
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        args = [
            "--prompt",
            "cover",
            "--output-dir",
            str(output),
            "--filename",
            "cover",
            "--max-attempts",
            "1",
        ]
        if json_mode:
            args.insert(0, "--json")
        exit_code = runtime.main(
            args,
            api_transport=api,
            download_transport=download,
            resolver=lambda host, port: ["8.8.8.8"],
            sleeper=lambda seconds: None,
            publisher=publisher,
            environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
            cwd=output.parent,
            home=output.parent / "home",
            stdout=stdout,
            stderr=stderr,
        )
        document = json.loads(stdout.getvalue()) if json_mode else None
        if document is not None:
            contract_errors = contract.validate_document(
                document,
                ["test-runtime-primary-key"],
            )
            if contract_errors:
                self.fail(f"runtime result violated Ticket 34: {contract_errors}")
        return exit_code, document, stdout.getvalue(), stderr.getvalue()

    def test_http_interruption_and_publication_failure_leave_no_partial_file(self):
        def interrupted(request, sink):
            sink.write(b"partial-body")
            raise OSError("interrupted")

        failures = [
            RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(404, (), b"not-found-body")]}
            ),
            RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(500, (), b"error-body")]}
            ),
            RoutedDownloadTransport(
                {"https://images.example.test/one.png": [interrupted]}
            ),
        ]
        for download in failures:
            with self.subTest(download=type(download).__name__), tempfile.TemporaryDirectory() as root:
                output = Path(root) / "outside-output"
                exit_code, document, _, _ = self.invoke_download(download, output)
                self.assertEqual(exit_code, 1)
                self.assertEqual(document["status"], "partial_success")
                self.assertEqual(document["outputs"][0]["status"], "download_failed")
                self.assertIsNone(document["outputs"][0]["local_path"])
                self.assertFalse((output / "cover.png").exists())
                self.assertEqual(list(output.glob(".image-2-download-*.tmp")), [])

        def failed_publisher(temp_path, final_path):
            raise OSError("atomic publication failed")

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "outside-output"
            download = RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(200, (), b"complete-image")]}
            )
            exit_code, document, _, _ = self.invoke_download(
                download,
                output,
                publisher=failed_publisher,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(document["outputs"][0]["status"], "download_failed")
            self.assertFalse((output / "cover.png").exists())
            self.assertEqual(list(output.glob(".image-2-download-*.tmp")), [])

    def test_atomic_publish_never_replaces_a_concurrently_created_final(self):
        def concurrent_publisher(temp_path, final_path):
            final_path.write_bytes(b"concurrent-writer")
            runtime.publish_no_replace(temp_path, final_path)

        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "outside-output"
            download = RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(200, (), b"our-complete-image")]}
            )
            exit_code, document, _, _ = self.invoke_download(
                download,
                output,
                publisher=concurrent_publisher,
            )
            self.assertEqual(exit_code, 1)
            self.assertEqual(document["outputs"][0]["status"], "download_failed")
            self.assertIsNone(document["outputs"][0]["local_path"])
            self.assertEqual((output / "cover.png").read_bytes(), b"concurrent-writer")
            self.assertEqual(list(output.glob(".image-2-download-*.tmp")), [])

    def test_multi_output_preserves_order_and_only_exposes_complete_files(self):
        first_url = "https://images.example.test/first.png"
        second_url = "https://images.example.test/second.png"
        api = FakeApiTransport(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-multi-output"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    json.dumps(
                        {
                            "status": "completed",
                            "results": [
                                {"url": first_url, "content_type": "image/png"},
                                {"url": second_url, "content_type": "image/png"},
                            ],
                        }
                    ).encode("utf-8"),
                ),
            ]
        )
        download = RoutedDownloadTransport(
            {
                first_url: [(200, (), b"first-complete")],
                second_url: [(503, (), b"second-error")],
            }
        )
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as external:
            external_output = Path(external) / "output with spaces"
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = runtime.main(
                [
                    "--json",
                    "--prompt",
                    "cover",
                    "--output-dir",
                    str(external_output),
                    "--filename",
                    "cover",
                    "--max-attempts",
                    "1",
                ],
                api_transport=api,
                download_transport=download,
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=lambda seconds: None,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
            document = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 1)
            self.assertEqual([item["index"] for item in document["outputs"]], [1, 2])
            self.assertEqual(
                [item["status"] for item in document["outputs"]],
                ["saved", "download_failed"],
            )
            first_path = Path(document["outputs"][0]["local_path"])
            self.assertEqual(first_path, external_output / "cover-01.png")
            self.assertEqual(first_path.read_bytes(), b"first-complete")
            self.assertIsNone(document["outputs"][1]["local_path"])
            self.assertFalse((external_output / "cover-02.png").exists())
            self.assertEqual(list(external_output.glob(".image-2-download-*.tmp")), [])

    def test_existing_final_gets_a_unique_name_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "outside-output"
            output.mkdir()
            existing = output / "cover.png"
            existing.write_bytes(b"existing")
            download = RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(200, (), b"new-image")]}
            )
            exit_code, document, _, _ = self.invoke_download(download, output)
            self.assertEqual(exit_code, 0)
            self.assertEqual(existing.read_bytes(), b"existing")
            new_path = Path(document["outputs"][0]["local_path"])
            self.assertEqual(new_path, output / "cover-2.png")
            self.assertEqual(new_path.read_bytes(), b"new-image")

    def test_legacy_mode_uses_the_same_failure_rule_without_saved_only_claim(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "outside-output"
            download = RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(500, (), b"error-body")]}
            )
            exit_code, _, stdout, stderr = self.invoke_download(
                download,
                output,
                json_mode=False,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("Result 1: https://images.example.test/one.png", stdout)
            self.assertNotIn("[save] Saved file(s):", stdout)
            self.assertIn("Completed with failures: saved=0 failed=1", stderr)
            self.assertFalse((output / "cover.png").exists())
            self.assertEqual(list(output.glob(".image-2-download-*.tmp")), [])

    def test_legacy_mode_reports_full_and_partial_saves_without_false_success(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "full-output"
            full = RoutedDownloadTransport(
                {"https://images.example.test/one.png": [(200, (), b"complete")]}
            )
            exit_code, _, stdout, stderr = self.invoke_download(
                full,
                output,
                json_mode=False,
            )
            self.assertEqual(exit_code, 0)
            self.assertIn("[save] Saved file(s):", stdout)
            self.assertNotIn("Completed with failures", stderr)
            self.assertEqual((output / "cover.png").read_bytes(), b"complete")

        first = "https://images.example.test/first.png"
        second = "https://images.example.test/second.png"
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "partial-output"
            partial = RoutedDownloadTransport(
                {
                    first: [(200, (), b"first-complete")],
                    second: [(503, (), b"second-error")],
                }
            )
            exit_code, _, stdout, stderr = self.invoke_download(
                partial,
                output,
                json_mode=False,
                results=[
                    {"url": first, "content_type": "image/png"},
                    {"url": second, "content_type": "image/png"},
                ],
            )
            self.assertEqual(exit_code, 1)
            self.assertNotIn("[save] Saved file(s):", stdout)
            self.assertIn("Completed with failures: saved=1 failed=1", stderr)
            self.assertEqual((output / "cover-01.png").read_bytes(), b"first-complete")
            self.assertFalse((output / "cover-02.png").exists())


class NetworkJitterTests(unittest.TestCase):
    """ADR 0006：瞬时错误分类、重试、退避，以及计费写操作的 ambiguous 保护。

    全部走注入的假 transport，不发任何真实网络请求。
    """

    def invoke(self, responses, *, download=None, max_attempts="1", extra=()):
        api = FakeApiTransport(responses)
        download = FakeDownloadTransport() if download is None else download
        stdout = io.StringIO()
        stderr = io.StringIO()
        sleeps = []
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as output:
            exit_code = runtime.main(
                [
                    "--json",
                    "--prompt",
                    "cover",
                    "--output-dir",
                    output,
                    "--poll-interval",
                    "9",
                    "--max-attempts",
                    max_attempts,
                    *extra,
                ],
                api_transport=api,
                download_transport=download,
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=sleeps.append,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
            document = json.loads(stdout.getvalue())
        return exit_code, document, stderr.getvalue(), api, download, sleeps

    @staticmethod
    def _send_failed():
        return runtime.TransportFailure("boom", runtime.TRANSPORT_SEND_FAILED)

    @staticmethod
    def _response_lost():
        return runtime.TransportFailure("boom", runtime.TRANSPORT_RESPONSE_LOST)

    def test_create_retries_when_the_request_never_left_the_client(self):
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                self._send_failed(),
                runtime.HttpResponse(200, (), b'{"id":"task-retry-send"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["task_id"], "task-retry-send")
        self.assertEqual(len(api.requests), 3)  # 失败的 create + 重试的 create + 1 次轮询
        self.assertEqual(sleeps[0], 1.0)  # 第 1 次重试等 1s（指数退避起点）

    def test_create_does_not_replay_when_the_response_was_lost(self):
        exit_code, document, stderr, api, _, sleeps = self.invoke(
            [self._response_lost(), runtime.HttpResponse(200, (), b'{"id":"never-reached"}')]
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["error"]["code"], "create_transport_error")
        self.assertIn("may or may not have been created", document["error"]["message"])
        self.assertEqual(len(api.requests), 1)  # 绝不重发，避免重复扣费
        self.assertEqual(sleeps, [])
        self.assertIn("the result is unknown and was NOT retried", stderr)

    def test_create_honours_retry_after_on_429(self):
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(429, (("Retry-After", "5"),), b'{"error":"slow down"}'),
                runtime.HttpResponse(200, (), b'{"id":"task-429"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["task_id"], "task-429")
        self.assertEqual(sleeps[0], 5.0)  # 按 Retry-After，而不是退避公式

    def test_poll_jitter_does_not_consume_the_polling_budget(self):
        # max_attempts=1：轮询预算只有一轮。中间两次物理请求抖动只消耗内层重试，
        # 第三次拿到 completed —— 旧实现会在第一次抖动就 query_transport_error。
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-poll-jitter"}'),
                self._response_lost(),
                self._response_lost(),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ],
            max_attempts="1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(sleeps, [1.0, 2.0])  # 内层退避 1s、2s；轮询间隔 9s 没被用到

    def test_poll_fails_only_after_the_inner_retries_are_exhausted(self):
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-poll-dead"}'),
                self._response_lost(),
                self._response_lost(),
                self._response_lost(),
            ],
            max_attempts="1",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["error"]["code"], "query_transport_error")
        self.assertEqual(len(api.requests), 4)  # 1 次 create + 3 次轮询尝试

    def test_download_retries_a_transient_hop_and_publishes_the_complete_body(self):
        url = "https://images.example.test/a.png?signature=x"
        download = RoutedDownloadTransport(
            {
                url: [
                    runtime.TransportFailure("hop reset", runtime.TRANSPORT_RESPONSE_LOST),
                    (200, (), b"complete-image"),
                ]
            }
        )
        api = FakeApiTransport(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-download-retry"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"'
                    + url.encode()
                    + b'","content_type":"image/png"}]}',
                ),
            ]
        )
        stdout = io.StringIO()
        stderr = io.StringIO()
        sleeps = []
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as output:
            exit_code = runtime.main(
                [
                    "--json", "--prompt", "cover", "--output-dir", output,
                    "--poll-interval", "9", "--max-attempts", "1",
                ],
                api_transport=api,
                download_transport=download,
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=sleeps.append,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
            document = json.loads(stdout.getvalue())
            saved = Path(document["outputs"][0]["local_path"])
            self.assertEqual(saved.read_bytes(), b"complete-image")
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["outputs"][0]["status"], "saved")
        self.assertEqual(len(download.requests), 2)
        self.assertEqual(sleeps, [1.0])

    def test_create_does_not_replay_a_5xx(self):
        """回归 H1c：5xx 下上游可能已受理并计费，状态码路径也不许重发。"""
        exit_code, document, stderr, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(503, (), b'raw create body'),
                runtime.HttpResponse(200, (), b'{"id":"never-reached"}'),
            ]
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["error"]["code"], "create_transport_error")
        self.assertIn("may or may not have been created", document["error"]["message"])
        self.assertEqual(len(api.requests), 1)  # 绝不重发
        self.assertEqual(sleeps, [])
        self.assertIn("whether it accepted the request is unknown", stderr)
        self.assertNotIn("raw create body", json.dumps(document))

    def test_create_still_retries_a_429(self):
        """429 是唯一能确认「未受理、未扣费」的状态码，仍然可重试。"""
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(429, (), b'{"error":"slow down"}'),
                runtime.HttpResponse(200, (), b'{"id":"task-429-retry"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["task_id"], "task-429-retry")
        self.assertEqual(sleeps[0], 1.0)

    def test_polling_still_retries_5xx_because_reads_are_idempotent(self):
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-poll-5xx"}'),
                runtime.HttpResponse(503, (), b'{"error":"upstream"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ],
            max_attempts="1",
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(len(api.requests), 3)
        self.assertEqual(sleeps, [1.0])

    def test_polling_stops_when_the_wall_clock_budget_is_spent(self):
        """回归 D2：次数还没用完，但墙钟预算耗尽就落 timed_out 终态。"""
        clock = {"t": 0.0}

        def monotonic():
            return clock["t"]

        class SlowApi(FakeApiTransport):
            def request(self, method, url, headers, body):  # noqa: ANN001
                clock["t"] += 200.0  # 每轮「实际」耗时 200s
                return super().request(method, url, headers, body)

        api = SlowApi(
            [runtime.HttpResponse(200, (), b'{"id":"task-wallclock"}')]
            + [runtime.HttpResponse(200, (), b'{"status":"processing"}')] * 20
        )
        stdout, stderr, sleeps = io.StringIO(), io.StringIO(), []
        with tempfile.TemporaryDirectory() as project, tempfile.TemporaryDirectory() as output:
            exit_code = runtime.main(
                [
                    "--json", "--prompt", "cover", "--output-dir", output,
                    "--poll-interval", "8", "--max-attempts", "20",
                ],
                api_transport=api,
                download_transport=RoutedDownloadTransport({}),
                resolver=lambda host, port: ["8.8.8.8"],
                sleeper=sleeps.append,
                monotonic=monotonic,
                environ={"AIHUB_API_KEY": "test-runtime-primary-key"},
                cwd=Path(project),
                home=Path(project) / "home",
                stdout=stdout,
                stderr=stderr,
            )
            document = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 3)
        self.assertEqual(document["status"], "timed_out")
        self.assertEqual(document["error"]["code"], "poll_timeout")
        # 计数预算 20 轮，但墙钟预算 20 × 8s = 160s 只够 1 轮（每轮「耗时」200s）：
        # 1 次 create + 1 次轮询后 deadline 到期，剩下 19 轮不再发。
        self.assertEqual(len(api.requests), 2)
        self.assertIn("wall-clock budget of 160s is spent", stderr.getvalue())

    def test_wall_clock_budget_matches_the_documented_default(self):
        self.assertEqual(runtime.DEFAULT_POLL_WALL_CLOCK_BUDGET_SECONDS, 720)
        parser_defaults = runtime.build_parser().parse_args(["--prompt", "x"])
        self.assertEqual(
            int(parser_defaults.max_attempts) * int(parser_defaults.poll_interval),
            runtime.DEFAULT_POLL_WALL_CLOCK_BUDGET_SECONDS,
        )

    def test_retry_after_parsing_is_bounded_and_falls_back_to_backoff(self):
        self.assertEqual(runtime._retry_after_seconds((("Retry-After", "3"),)), 3.0)
        self.assertIsNone(runtime._retry_after_seconds((("Retry-After", "Wed, 21 Oct 2026 07:28:00 GMT"),)))
        self.assertIsNone(runtime._retry_after_seconds(()))
        self.assertEqual(
            runtime._retry_after_seconds((("Retry-After", "99999"),)),
            float(runtime.MAX_RETRY_AFTER_SECONDS),
        )
        # 回归 L1：float("nan") 不报错，但 min(nan, 120) 仍是 nan，sleep(nan) 会崩。
        for raw in ("nan", "NaN", "inf", "-inf", "Infinity"):
            self.assertIsNone(
                runtime._retry_after_seconds(((("Retry-After"), raw),)), raw
            )
        self.assertIsNone(runtime._retry_after_seconds((("Retry-After", "-1"),)))

    def test_nan_retry_after_falls_back_to_backoff_end_to_end(self):
        """nan 必须在到达 sleeper 之前就被挡掉，等待时长回落到指数退避。"""
        exit_code, document, _, _, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(429, (("Retry-After", "nan"),), b'{"error":"slow"}'),
                runtime.HttpResponse(200, (), b'{"id":"task-nan"}'),
                runtime.HttpResponse(
                    200,
                    (),
                    b'{"status":"completed","results":[{"url":"https://images.example.test/a.png?signature=x","content_type":"image/png"}]}',
                ),
            ]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(sleeps[0], 1.0)
        for wait in sleeps:
            self.assertEqual(wait, wait, "等待时长不得为 nan")
        self.assertEqual(runtime._backoff_seconds(1), 1.0)
        self.assertEqual(runtime._backoff_seconds(2), 2.0)
        self.assertTrue(runtime._is_transient_status(429))
        self.assertTrue(runtime._is_transient_status(503))
        self.assertFalse(runtime._is_transient_status(401))
        self.assertFalse(runtime._is_transient_status(404))

    def test_deterministic_transport_failures_are_never_retried(self):
        exit_code, document, _, api, _, sleeps = self.invoke(
            [
                runtime.HttpResponse(200, (), b'{"id":"task-deterministic"}'),
                runtime.TransportFailure("oversize", runtime.TRANSPORT_DETERMINISTIC),
                runtime.HttpResponse(200, (), b'{"status":"completed"}'),
            ],
            max_attempts="1",
        )
        self.assertEqual(exit_code, 1)
        self.assertEqual(document["error"]["code"], "query_transport_error")
        self.assertEqual(len(api.requests), 2)  # create + 1 次轮询，确定性失败不重试
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
