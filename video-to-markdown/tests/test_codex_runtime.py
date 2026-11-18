import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from video_to_markdown import codex_launch, codex_transport, config


@contextmanager
def provider(handler):
    calls = []

    class MockProvider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self):
            data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            calls.append((self.command, self.path, self.headers.get("Authorization"), data))
            handler(self, len(calls))

        do_GET = do_POST = respond

    with HTTPServer(("127.0.0.1", 0), MockProvider) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "synthetic-provider-key"}, calls
        finally:
            server.shutdown()
            worker.join()


def sse(handler, *events):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.end_headers()
    for event in events:
        handler.wfile.write(b"data: " + json.dumps(event).encode() + b"\n\n")


def rejected(handler, *, cooldown=None):
    handler.send_response(429)
    handler.send_header("Content-Type", "application/json")
    if cooldown is not None:
        handler.send_header("Retry-After", cooldown)
    handler.end_headers()
    handler.wfile.write(b'{"error":{"code":"rate_limit_exceeded"}}')


def delegate(endpoint, token):
    request = urllib.request.Request(endpoint + "/responses", data=b'{"stream":true}', headers={
        "Authorization": "Bearer " + token,
    })
    with urllib.request.urlopen(request, timeout=4) as result:
        return result.read()


def test_independent_budgets_recover_the_same_response_without_submitting_a_second_generation():
    created = {"type":"response.created", "sequence_number":3, "response":{"id":"resp_existing"}}
    completed = {"type":"response.completed", "sequence_number":4, "response":{"id":"resp_existing"}}

    def interrupted(reply, index):
        if index == 1:
            rejected(reply)
        elif index == 4:
            sse(reply, created, completed)
        else:
            sse(reply, created)

    policy = codex_transport.Recovery(request_retries=1, stream_retries=2, backoff_base_ms=1,
                                      backoff_max_ms=2, responses_background=True)
    with provider(interrupted) as (route, calls), codex_transport.serve(route, policy, timeout=4) as (endpoint, token, runtime):
        output = delegate(endpoint, token)
        assert output.count(b"response.created") == 1 and b"response.completed" in output
        assert runtime.snapshot()["response_id"] == "resp_existing"
        assert runtime.snapshot()["request_retries"] == 1
        assert runtime.snapshot()["stream_reconnects"] == 2
        assert token != route["api_key"] and all(call[2] == "Bearer " + route["api_key"] for call in calls)
        assert [call[0] for call in calls] == ["POST", "POST", "GET", "GET"]
        assert json.loads(calls[1][3]) == {"stream": True, "background": True, "store": True}
        assert all(call[1] == "/v1/responses/resp_existing?stream=true&starting_after=3" for call in calls[2:])


def test_request_budget_exhaustion_counts_five_rejections_after_the_first_attempt():
    with provider(lambda reply, _: rejected(reply)) as (route, calls):
        policy = codex_transport.Recovery(backoff_base_ms=1, backoff_max_ms=1)
        with codex_transport.serve(route, policy, timeout=4) as (endpoint, token, runtime):
            with pytest.raises(urllib.error.HTTPError) as failure:
                delegate(endpoint, token)
            assert failure.value.code == 429
            assert len(calls) == 6 and runtime.snapshot()["request_retries"] == 5
            assert runtime.snapshot()["stream_reconnects"] == 0


def test_stream_budget_exhaustion_does_not_post_again_and_default_is_five():
    def interrupted(reply, _index):
        sse(reply, {"type": "response.created", "sequence_number": 0, "response": {"id": "resp_previous"}})

    policy = codex_transport.Recovery(backoff_base_ms=1, backoff_max_ms=1, responses_background=True)
    with provider(interrupted) as (route, calls), codex_transport.serve(route, policy, timeout=4) as (endpoint, token, runtime):
        assert b"response.completed" not in delegate(endpoint, token)
        assert runtime.snapshot()["stream_reconnects"] == 5
        assert [call[0] for call in calls] == ["POST", "GET", "GET", "GET", "GET", "GET"]


def test_retry_cooldown_larger_than_the_budget_survives_failure():
    with provider(lambda reply, _: rejected(reply, cooldown="30")) as (route, calls):
        with codex_transport.serve(route, codex_transport.Recovery(), timeout=4) as (endpoint, token, runtime):
            with pytest.raises(urllib.error.HTTPError) as failure:
                delegate(endpoint, token)
            assert failure.value.code == 429
            assert runtime.snapshot()["retry_not_before"] is not None
            assert len(calls) == 1


def test_a_gateway_error_or_ordinary_stream_end_does_not_replay_paid_work():
    def fail(reply, index):
        if index == 1:
            reply.send_error(502)
        elif index == 3:
            reply.send_error(404)
        else:
            sse(reply, {"type":"response.created", "sequence_number": 0, "response":{"id":"resp_without_storage"}})

    with provider(fail) as (route, calls), codex_transport.serve(route, codex_transport.Recovery(), timeout=4) as (endpoint, token, runtime):
        with pytest.raises(urllib.error.HTTPError):
            delegate(endpoint, token)
        assert len(calls) == 1 and runtime.snapshot()["outcome_unknown"]
        with pytest.raises(urllib.error.HTTPError) as repeated:
            delegate(endpoint, token)
        assert repeated.value.code == 409 and len(calls) == 1
        with codex_transport.serve(route, codex_transport.Recovery(), timeout=4) as (new_endpoint, new_token, new_runtime):
            assert b"response.created" in delegate(new_endpoint, new_token)
            assert new_runtime.snapshot()["response_id"] == "resp_without_storage"
            assert [call[0] for call in calls] == ["POST", "POST", "GET"]
            assert all("store" not in json.loads(call[3]) for call in calls[:2])


def test_runtime_settings_are_independent_and_respect_project_layers(tmp_path):
    env = {"VIDEO_TO_MARKDOWN_STREAM_MAX_RETRIES": "0"}
    (tmp_path / ".env").write_text("VIDEO_TO_MARKDOWN_REQUEST_MAX_RETRIES=3\n", encoding="utf-8")
    (tmp_path / ".env.video-to-markdown").write_text("VIDEO_TO_MARKDOWN_RETRY_BACKOFF_BASE_MS=20\n", encoding="utf-8")
    settings = config.codex_settings(tmp_path, environ=env)
    assert settings.recovery == codex_transport.Recovery(request_retries=3, stream_retries=0, backoff_base_ms=20)
    assert settings.sources["stream_retries"] == "process environment"
    assert settings.sources["request_retries"] == str(tmp_path / ".env")


def test_two_codex_installations_prioritize_explicit_then_matching_host_and_fall_back(tmp_path):
    name = "codex.exe" if os.name == "nt" else "codex"
    path_first = tmp_path / "PATH install" / name
    current = tmp_path / "Session install" / name
    for item in (path_first, current):
        item.parent.mkdir()
        item.touch(mode=0o755)
    args = {"environ": {"PATH": str(path_first.parent)}, "find_current": lambda _: current}
    assert codex_launch.resolve(tmp_path, host="codex", **args).argv[0] == str(current)
    assert Path(codex_launch.resolve(tmp_path, host="workbuddy", **args).argv[0]).samefile(path_first)
    assert codex_launch.resolve(tmp_path, explicit=str(path_first), host="codex", **args).source == "explicit configuration"
    with pytest.raises(codex_launch.LaunchError):
        codex_launch.resolve(tmp_path, explicit="absent/codex", **args)
    assert codex_launch.resolve(tmp_path, **{**args, "find_current": lambda _: None}).source == "PATH"
