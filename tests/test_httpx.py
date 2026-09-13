import io
import unittest
import urllib.error
from unittest import mock

from bot.httpx import HttpError, _safe, request


class FakeConnection:
    def __init__(self, status=200, body=b"{}", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code, body=b"boom"):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


class RequestTests(unittest.TestCase):
    def test_success_returns_parsed_json(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeConnection(body=b'{"a":1}')):
            response = request("GET", "http://x")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json(), {"a": 1})

    def test_json_body_sets_content_type(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["data"] = req.data
            return FakeConnection()

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            request("POST", "http://x", json_body={"k": "v"})

        self.assertEqual(captured["data"], b'{"k": "v"}')
        self.assertEqual(captured["headers"]["Content-type"], "application/json")

    def test_retries_on_500_then_succeeds(self):
        responses = [http_error(500), FakeConnection(body=b'{"ok":true}')]

        def fake_urlopen(req, timeout=None):
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        slept = []
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            response = request("GET", "http://x", retries=3, sleep=slept.append)

        self.assertEqual(response.json(), {"ok": True})
        self.assertEqual(len(slept), 1)

    def test_gives_up_after_retries_and_raises_with_status(self):
        with mock.patch("urllib.request.urlopen", side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(503))):
            with self.assertRaises(HttpError) as ctx:
                request("GET", "http://x", retries=2, sleep=lambda _: None)
        self.assertEqual(ctx.exception.status, 503)

    def test_client_errors_are_not_retried(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise http_error(400, b"bad request")

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(HttpError) as ctx:
                request("GET", "http://x", retries=3, sleep=lambda _: None)

        self.assertEqual(len(calls), 1)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bad request", str(ctx.exception))

    def test_retry_after_header_is_honoured(self):
        error = urllib.error.HTTPError("http://x", 429, "slow down", {"Retry-After": "3"}, io.BytesIO(b""))
        responses = [error, FakeConnection()]
        slept = []

        def fake_urlopen(req, timeout=None):
            nxt = responses.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            request("GET", "http://x", retries=2, sleep=slept.append)

        self.assertEqual(slept, [3.0])

    def test_network_errors_are_retried_then_wrapped(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")):
            with self.assertRaises(HttpError):
                request("GET", "http://x", retries=2, sleep=lambda _: None)


class RedactionTests(unittest.TestCase):
    def test_long_path_segments_are_masked(self):
        url = "https://api.green-api.com/waInstance1101/sendMessage/abcdef0123456789abcdef0123456789"
        masked = _safe(url)
        self.assertNotIn("abcdef0123456789abcdef0123456789", masked)
        self.assertIn("sendMessage", masked)


if __name__ == "__main__":
    unittest.main()
