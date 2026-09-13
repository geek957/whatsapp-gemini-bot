"""Meta WhatsApp Cloud API transport (1:1 conversations only).

Kept as the ToS-compliant fallback. Two hard limits shape this implementation:

1. The Cloud API has no group messaging, so this provider only ever sees direct chats.
2. It has no read/history endpoint at all -- messages arrive by webhook push. There is
   nothing to poll, so `fetch_messages` reads the webhook payload that a relay forwarded
   through `repository_dispatch` (see relay/cloudflare-worker.js). On a cron run with no
   dispatch payload it correctly returns nothing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..config import Config
from ..httpx import HttpError, get_bytes, request
from .base import InboundMessage, Provider

LOG = logging.getLogger(__name__)


class MetaCloudProvider(Provider):
    name = "meta"

    def __init__(self, cfg: Config, http=request, downloader=get_bytes, event_path: str | None = None):
        self.cfg = cfg
        self._http = http
        self._download = downloader
        self._event_path = event_path if event_path is not None else os.environ.get("GITHUB_EVENT_PATH", "")

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.meta_access_token}"}

    def fetch_messages(self) -> list[InboundMessage]:
        payload = self._client_payload()
        if not payload:
            LOG.info("meta provider: no repository_dispatch payload; nothing to read")
            return []
        return _parse_webhook(payload)

    def _client_payload(self) -> dict:
        if not self._event_path or not Path(self._event_path).is_file():
            return {}
        try:
            event = json.loads(Path(self._event_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.error("cannot read GITHUB_EVENT_PATH: %s", exc)
            return {}
        return (event.get("client_payload") or {}).get("webhook") or {}

    def download_image(self, message: InboundMessage) -> tuple[bytes, str]:
        media_id = message.download_url  # media id, resolved to a short-lived URL below
        if not media_id:
            raise HttpError(404, "media", b"no media id on message")
        meta = self._http(
            "GET", f"{self.cfg.meta_api_base}/{media_id}", headers=self._auth(),
            timeout=self.cfg.http_timeout, retries=self.cfg.http_retries,
        ).json() or {}
        url = meta.get("url")
        if not url:
            raise HttpError(404, "media", b"media lookup returned no url")
        data, content_type = self._download(
            url, headers=self._auth(), timeout=self.cfg.http_timeout,
            retries=self.cfg.http_retries, max_bytes=self.cfg.max_image_bytes,
        )
        return data, meta.get("mime_type") or message.mime_type or content_type

    def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        body: dict = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": chat_id,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        if reply_to and self.cfg.reply_mode == "quote":
            body["context"] = {"message_id": reply_to}
        payload = self._http(
            "POST", f"{self.cfg.meta_api_base}/{self.cfg.meta_phone_number_id}/messages",
            headers=self._auth(), json_body=body,
            timeout=self.cfg.http_timeout, retries=self.cfg.http_retries,
        ).json() or {}
        sent = payload.get("messages") or [{}]
        return str(sent[0].get("id", ""))


def _parse_webhook(payload: dict) -> list[InboundMessage]:
    """Flatten a Cloud API webhook body into InboundMessage records."""
    messages: list[InboundMessage] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            names = {c.get("wa_id"): (c.get("profile") or {}).get("name", "") for c in value.get("contacts") or []}
            for raw in value.get("messages") or []:
                message = _parse_message(raw, names)
                if message is not None:
                    messages.append(message)
    return sorted(messages, key=lambda m: m.timestamp)


def _parse_message(raw: dict, names: dict[str, str]) -> InboundMessage | None:
    message_id = raw.get("id") or ""
    sender = raw.get("from") or ""
    if not message_id or not sender:
        return None
    kind_raw = raw.get("type") or ""
    timestamp = int(raw.get("timestamp") or 0)
    common = {
        "id": message_id,
        "chat_id": sender,
        "timestamp": timestamp,
        "sender": sender,
        "sender_name": names.get(sender, ""),
        "raw": raw,
    }
    if kind_raw == "image":
        image = raw.get("image") or {}
        return InboundMessage(
            kind="image",
            text=(image.get("caption") or "").strip(),
            mime_type=image.get("mime_type") or "",
            download_url=image.get("id") or "",
            **common,
        )
    if kind_raw == "text":
        return InboundMessage(kind="text", text=((raw.get("text") or {}).get("body") or "").strip(), **common)
    return InboundMessage(kind="other", **common)
