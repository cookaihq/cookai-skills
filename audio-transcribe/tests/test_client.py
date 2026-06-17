import client


def test_call_with_key_fallback_advances_only_on_401():
    calls = []
    def attempt(k):
        calls.append(k)
        return client.Resp(401 if k == "k1" else 200, {"ok": True}, "")
    resp, used = client.call_with_key_fallback(["k1", "k2"], attempt)
    assert used == "k2" and resp.status == 200 and calls == ["k1", "k2"]


def test_call_with_key_fallback_stops_on_non_401():
    def attempt(k):
        return client.Resp(402, {"e": "no credit"}, "")
    resp, used = client.call_with_key_fallback(["k1", "k2"], attempt)
    assert used == "k1" and resp.status == 402


def test_http_request_injects_browser_ua(monkeypatch):
    seen = {}
    class FakeResp:
        status = 200
        def read(self): return b'{"x":1}'
        def __enter__(self): return self
        def __exit__(self, *a): return False
    def fake_urlopen(req, timeout=0):
        seen["ua"] = req.headers.get("User-agent")  # urllib title-cases header keys
        return FakeResp()
    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    r = client.http_request("GET", "https://x", {"Authorization": "Bearer k"})
    assert r.status == 200 and r.json == {"x": 1}
    assert seen["ua"] and "Mozilla" in seen["ua"]
