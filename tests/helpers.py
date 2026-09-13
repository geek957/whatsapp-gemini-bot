"""Shared fakes for the test suite."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from bot.config import Config
from bot.httpx import HttpError, Response
from bot.providers.base import InboundMessage, Provider

REPO_ROOT = Path(__file__).resolve().parents[1]


def make_config(**overrides) -> Config:
    base = {
        "provider": "greenapi",
        "green_instance_id": "1101",
        "green_token": "tok",
        "gemini_api_key": "key",
        "prompt_path": REPO_ROOT / "prompts" / "default.md",
        "chat_ids": ("123-456@g.us",),
        "trigger_mode": "any_image",
        "read_mode": "queue",
        "http_retries": 1,
    }
    base.update(overrides)
    return Config(**base)


def image_message(message_id: str, *, chat_id: str = "123-456@g.us", caption: str = "",
                  timestamp: int = 1_700_000_000, url: str = "https://media/x.jpg",
                  mime: str = "image/jpeg") -> InboundMessage:
    return InboundMessage(
        id=message_id, chat_id=chat_id, kind="image", timestamp=timestamp,
        text=caption, chat_name="Group", sender="7900@c.us", sender_name="Bob",
        mime_type=mime, download_url=url,
    )


def text_message(message_id: str, text: str, *, chat_id: str = "123-456@g.us",
                 timestamp: int = 1_700_000_000) -> InboundMessage:
    return InboundMessage(
        id=message_id, chat_id=chat_id, kind="text", timestamp=timestamp,
        text=text, chat_name="Group", sender="7900@c.us", sender_name="Bob",
    )


@dataclass
class FakeProvider(Provider):
    name: str = "fake"
    inbound: list[InboundMessage] = field(default_factory=list)
    sent: list[tuple[str, str, str | None]] = field(default_factory=list)
    downloads: dict[str, tuple[bytes, str]] = field(default_factory=dict)
    fail_download: set[str] = field(default_factory=set)
    fail_send: bool = False
    fail_fetch: bool = False

    def fetch_messages(self) -> list[InboundMessage]:
        if self.fail_fetch:
            raise HttpError(503, "fetch", b"upstream down")
        return list(self.inbound)

    def download_image(self, message: InboundMessage) -> tuple[bytes, str]:
        if message.id in self.fail_download:
            raise HttpError(404, "download", b"gone")
        return self.downloads.get(message.id, (b"\x89PNG-bytes", message.mime_type or "image/jpeg"))

    def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        if self.fail_send:
            raise HttpError(500, "send", b"nope")
        self.sent.append((chat_id, text, reply_to))
        return f"sent-{len(self.sent)}"


class FakeHttp:
    """Records requests and replays queued responses, matching bot.httpx.request."""

    def __init__(self, responses: list[Response | Exception] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers=None, json_body=None, timeout=30, retries=3, sleep=None):
        self.calls.append({
            "method": method, "url": url, "headers": headers or {},
            "json": json_body, "timeout": timeout, "retries": retries,
        })
        if not self.responses:
            return json_response({})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def json_response(payload, status: int = 200) -> Response:
    return Response(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})


def null_response() -> Response:
    """Green API returns a bare `null` body when the notification queue is empty."""
    return Response(200, b"null", {"Content-Type": "application/json"})
