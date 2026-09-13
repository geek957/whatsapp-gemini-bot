"""End-to-end test over real HTTP against stub Green API and Gemini servers.

Exercises the actual CLI, urllib transport, state file and reply path, which the unit
tests deliberately bypass. Nothing external is contacted: both stubs bind to localhost.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bot.cli import main

REPO_ROOT = Path(__file__).resolve().parents[1]

IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"


class StubHandler(BaseHTTPRequestHandler):
    """One stub for the Green API, media host and Gemini, routed by path."""

    requests: list[tuple[str, str, dict]] = []
    queue: list[dict] = []

    def log_message(self, *args):  # keep the test output clean
        pass

    def _record(self, body: dict | None = None):
        type(self).requests.append((self.command, self.path, body or {}))

    def _json(self, payload, status=200):
        blob = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        self._record()
        if "/receiveNotification/" in self.path:
            self._json(type(self).queue.pop(0) if type(self).queue else None)
        elif self.path.startswith("/media/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(IMAGE_BYTES)))
            self.end_headers()
            self.wfile.write(IMAGE_BYTES)
        else:
            self._json({}, 404)

    def do_DELETE(self):
        self._record()
        self._json({"result": True})

    def do_POST(self):
        body = self._read_body()
        self._record(body)
        if self.path.endswith(":generateContent"):
            # Assert the image really travelled as base64 inline data.
            parts = body["contents"][0]["parts"]
            assert parts[1]["inline_data"]["data"] == base64.b64encode(IMAGE_BYTES).decode()
            assert parts[1]["inline_data"]["mime_type"] == "image/png"
            self._json({
                "candidates": [{"content": {"parts": [{"text": "Total is 42.50 EUR."}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 250, "candidatesTokenCount": 8},
            })
        elif "/sendMessage/" in self.path:
            self._json({"idMessage": "REPLY-1"})
        else:
            self._json({}, 404)


def notification(port: int) -> dict:
    return {
        "receiptId": 1,
        "body": {
            "typeWebhook": "incomingMessageReceived",
            "idMessage": "MSG-INT-1",
            # Must be inside WINDOW_MINUTES or the pipeline correctly treats it as stale.
            "timestamp": int(time.time()),
            "senderData": {"chatId": "111-222@g.us", "chatName": "Test Group",
                           "sender": "49150@c.us", "senderName": "Ada"},
            "messageData": {
                "typeMessage": "imageMessage",
                "fileMessageData": {
                    "downloadUrl": f"http://127.0.0.1:{port}/media/photo.png",
                    "caption": "/ask what is the total?",
                    "mimeType": "image/png",
                    "fileName": "photo.png",
                },
            },
        },
    }


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        StubHandler.requests = []
        StubHandler.queue = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmp.name) / "state.json"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _env(self, **overrides) -> dict[str, str]:
        base = f"http://127.0.0.1:{self.port}"
        env = {
            "PROVIDER": "greenapi",
            "GREEN_API_INSTANCE_ID": "1101",
            "GREEN_API_TOKEN": "test-token",
            "GREEN_API_BASE": base,
            "GREEN_API_MEDIA_BASE": base,
            "GEMINI_API_KEY": "test-key",
            "GEMINI_API_BASE": base,
            "GEMINI_MODEL": "gemini-2.5-flash",
            "WHATSAPP_CHAT_IDS": "111-222@g.us",
            "TRIGGER_MODE": "command",
            "READ_MODE": "queue",
            "STATE_PATH": str(self.state_path),
            "PROMPT_PATH": str(REPO_ROOT / "prompts" / "default.md"),
            "HTTP_RETRIES": "1",
            "LOG_LEVEL": "CRITICAL",
        }
        env.update(overrides)
        return env

    def _run(self, argv, **env_overrides) -> int:
        from unittest import mock

        with mock.patch.dict("os.environ", self._env(**env_overrides), clear=True):
            return main(argv)

    def test_full_cycle_reads_downloads_analyses_and_replies(self):
        StubHandler.queue = [notification(self.port)]

        exit_code = self._run(["run"])
        self.assertEqual(exit_code, 0)

        paths = [path for _, path, _ in StubHandler.requests]
        self.assertTrue(any("/receiveNotification/" in p for p in paths))
        self.assertTrue(any("/deleteNotification/" in p for p in paths))
        self.assertTrue(any(p.startswith("/media/") for p in paths))
        self.assertTrue(any(p.endswith(":generateContent") for p in paths))

        sends = [body for method, path, body in StubHandler.requests if "/sendMessage/" in path]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["chatId"], "111-222@g.us")
        self.assertEqual(sends[0]["message"], "Total is 42.50 EUR.")
        self.assertEqual(sends[0]["quotedMessageId"], "MSG-INT-1")

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("MSG-INT-1", state["processed"])
        self.assertEqual(state["runs"], 1)

    def test_question_from_caption_reaches_the_model(self):
        StubHandler.queue = [notification(self.port)]
        self._run(["run"])
        gemini = [body for _, path, body in StubHandler.requests if path.endswith(":generateContent")][0]
        prompt = gemini["contents"][0]["parts"][0]["text"]
        self.assertIn("what is the total?", prompt)
        self.assertIn("WhatsApp", prompt, "the prompt file should be included")

    def test_rerun_after_state_written_sends_nothing(self):
        StubHandler.queue = [notification(self.port)]
        self._run(["run"])
        StubHandler.requests = []
        StubHandler.queue = [notification(self.port)]  # provider redelivers the same message

        self._run(["run"])

        sends = [path for _, path, _ in StubHandler.requests if "/sendMessage/" in path]
        self.assertEqual(sends, [], "dedupe state must prevent a duplicate reply")

    def test_dry_run_touches_neither_gemini_nor_send(self):
        StubHandler.queue = [notification(self.port)]
        exit_code = self._run(["run", "--dry-run"], DRY_RUN="true")
        self.assertEqual(exit_code, 0)
        paths = [path for _, path, _ in StubHandler.requests]
        self.assertFalse(any(p.endswith(":generateContent") for p in paths))
        self.assertFalse(any("/sendMessage/" in p for p in paths))

    def test_missing_credentials_exit_code_is_two(self):
        from unittest import mock

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(main(["run"]), 2)

    def test_empty_queue_is_a_clean_no_op(self):
        exit_code = self._run(["run"])
        self.assertEqual(exit_code, 0)
        sends = [path for _, path, _ in StubHandler.requests if "/sendMessage/" in path]
        self.assertEqual(sends, [])


if __name__ == "__main__":
    unittest.main()
