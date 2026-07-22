import importlib.util
import io
import sys
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "image_task.py"
SPEC = importlib.util.spec_from_file_location("image_2_standard_transport", SCRIPT_PATH)
runtime = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runtime
SPEC.loader.exec_module(runtime)


class FakeHttpResponse:
    def __init__(self, body, *, remaining_after_eof=0):
        self.status = 200
        self._body = body
        self._read = False
        self._remaining_after_eof = remaining_after_eof
        self.length = len(body) + remaining_after_eof

    def getheaders(self):
        return [("Content-Length", str(len(self._body) + self._remaining_after_eof))]

    def read(self, size):
        if self._read:
            self.length = self._remaining_after_eof
            return b""
        self._read = True
        self.length = self._remaining_after_eof
        return self._body

    def close(self):
        pass


class FakeConnection:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    def request(self, method, target, headers):
        self.requests.append((method, target, dict(headers)))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class StandardTransportTests(unittest.TestCase):
    def request(self):
        return runtime.DownloadRequest(
            url="https://images.example.test:8443/output.png?signature=independent",
            pinned_address="8.8.8.8",
            server_hostname="images.example.test",
            port=8443,
            request_target="/output.png?signature=independent",
            headers=(("Accept", "*/*"), ("User-Agent", "image-2/2")),
        )

    def test_connection_factory_receives_pinned_ip_and_original_tls_hostname(self):
        connection = FakeConnection(FakeHttpResponse(b"complete-image"))
        factory_calls = []

        def factory(host, port, pinned_address, timeout):
            factory_calls.append((host, port, pinned_address, timeout))
            return connection

        transport = runtime.StandardDownloadTransport(
            timeout=17,
            connection_factory=factory,
        )
        sink = io.BytesIO()
        response = transport.fetch(self.request(), sink)

        self.assertEqual(factory_calls, [("images.example.test", 8443, "8.8.8.8", 17)])
        self.assertEqual(response.status, 200)
        self.assertEqual(sink.getvalue(), b"complete-image")
        self.assertTrue(connection.closed)
        headers = {key.lower(): value for key, value in connection.requests[0][2].items()}
        self.assertNotIn("authorization", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("referer", headers)

    def test_declared_content_length_truncation_is_a_transport_failure(self):
        connection = FakeConnection(
            FakeHttpResponse(b"partial", remaining_after_eof=5)
        )
        transport = runtime.StandardDownloadTransport(
            connection_factory=lambda host, port, pinned_address, timeout: connection
        )
        with self.assertRaises(runtime.TransportFailure):
            transport.fetch(self.request(), io.BytesIO())
        self.assertTrue(connection.closed)


if __name__ == "__main__":
    unittest.main()
