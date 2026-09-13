"""Green API transport: pull-queue reads plus group-capable sends.

Green API is an unofficial WhatsApp gateway. It is used here because it is the only
option that (a) supports group chats and (b) exposes a *pull* read endpoint, so an
ephemeral GitHub Actions runner needs no public webhook endpoint and no persistent socket.

Payload field names follow Green API's documented `incomingMessageReceived` shape. The
parser is deliberately tolerant of missing or renamed fields so a provider-side change
degrades to "message skipped" rather than a crash; run `python -m bot.cli doctor --raw`
to dump a live notification and confirm the shape against your instance.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Config
from ..httpx import HttpError, get_bytes, request
from .base import InboundMessage, Provider

LOG = logging.getLogger(__name__)

IMAGE_TYPES = frozenset({"imageMessage", "stickerMessage"})
TEXT_TYPES = frozenset({"textMessage", "extendedTextMessage", "quotedMessage"})

# `outgoingMessageReceived` is a message you sent from the phone; `outgoingAPIMessageReceived`
# is one this bot sent through the API. The latter is never processed, which is what keeps the
# bot from answering its own replies in a loop.
INCOMING_HOOK = "incomingMessageReceived"
OUTGOING_PHONE_HOOK = "outgoingMessageReceived"
SELF_SENT_HOOK = "outgoingAPIMessageReceived"


class GreenApiProvider(Provider):
    name = "greenapi"

    def __init__(self, cfg: Config, http=request, downloader=get_bytes):
        self.cfg = cfg
        self._http = http
        self._download = downloader
        self._pending_receipts: list[int] = []

    # ---------------------------------------------------------------- endpoints

    def _url(self, method: str, *suffix: str, media: bool = False) -> str:
        base = self.cfg.green_media_base if media else self.cfg.green_api_base
        parts = "/".join(str(part) for part in suffix)
        tail = f"/{parts}" if parts else ""
        return f"{base}/waInstance{self.cfg.green_instance_id}/{method}/{self.cfg.green_token}{tail}"

    def _call(self, method: str, endpoint: str, *suffix: str, body: Any = None, media: bool = False) -> Any:
        resp = self._http(
            method,
            self._url(endpoint, *suffix, media=media),
            json_body=body,
            timeout=self.cfg.http_timeout,
            retries=self.cfg.http_retries,
        )
        return resp.json()

    # -------------------------------------------------------------------- reads

    def fetch_messages(self) -> list[InboundMessage]:
        messages: list[InboundMessage] = []
        if self.cfg.read_mode in {"queue", "both"}:
            messages.extend(self._drain_queue())
        if self.cfg.read_mode in {"history", "both"}:
            messages.extend(self._sweep_history())
        return _dedupe(messages)

    def _drain_queue(self) -> list[InboundMessage]:
        """Consume the notification queue until empty or the run budget is spent."""
        found: list[InboundMessage] = []
        # A generous cap: the queue is drained fully in normal operation, but a
        # long backlog must not spin forever inside a 5-minute schedule.
        for _ in range(200):
            try:
                payload = self._call("GET", "receiveNotification")
            except HttpError as exc:
                LOG.error("receiveNotification failed: %s", exc)
                break
            if not payload:
                break
            receipt = payload.get("receiptId")
            message = _parse_notification(payload.get("body") or {}, self.cfg.include_outgoing)
            if message is not None:
                found.append(message)
            if receipt is not None:
                self._pending_receipts.append(int(receipt))
                # Delete immediately: Green API redelivers un-deleted notifications,
                # and state.json is the durable dedupe record, not the queue.
                self._delete_notification(int(receipt))
        return found

    def _delete_notification(self, receipt_id: int) -> None:
        try:
            self._call("DELETE", "deleteNotification", str(receipt_id))
        except HttpError as exc:
            LOG.warning("deleteNotification %s failed: %s", receipt_id, exc)

    def _sweep_history(self) -> list[InboundMessage]:
        """Re-read recent history for the configured chats to catch dropped queue items."""
        found: list[InboundMessage] = []
        if not self.cfg.chat_ids:
            LOG.info("READ_MODE includes history but WHATSAPP_CHAT_IDS is empty; skipping sweep")
            return found
        for chat_id in self.cfg.chat_ids:
            try:
                rows = self._call("POST", "getChatHistory", body={"chatId": chat_id, "count": self.cfg.history_count})
            except HttpError as exc:
                LOG.error("getChatHistory(%s) failed: %s", chat_id, exc)
                continue
            for row in rows or []:
                message = _parse_history_row(row, chat_id, self.cfg.include_outgoing)
                if message is not None:
                    found.append(message)
        return found

    def download_image(self, message: InboundMessage) -> tuple[bytes, str]:
        url = message.download_url
        if not url:
            payload = self._call("POST", "downloadFile", body={"chatId": message.chat_id, "idMessage": message.id})
            url = (payload or {}).get("downloadUrl", "")
        if not url:
            raise HttpError(404, "downloadFile", b"no downloadUrl for message")
        data, content_type = self._download(
            url,
            timeout=self.cfg.http_timeout,
            retries=self.cfg.http_retries,
            max_bytes=self.cfg.max_image_bytes,
        )
        return data, message.mime_type or content_type

    # ------------------------------------------------------------------- writes

    def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        body: dict[str, Any] = {"chatId": chat_id, "message": text}
        if reply_to and self.cfg.reply_mode == "quote":
            body["quotedMessageId"] = reply_to
        payload = self._call("POST", "sendMessage", body=body)
        return str((payload or {}).get("idMessage", ""))

    # -------------------------------------------------------------- diagnostics

    def state_instance(self) -> dict:
        """Instance authorisation state: `authorized` means the number is linked."""
        return self._call("GET", "getStateInstance") or {}

    def list_chats(self) -> list[dict]:
        """Every known chat, so a group id can be found without waiting for a message."""
        rows = self._call("GET", "getContacts") or []
        chats = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            chat_id = row.get("id") or ""
            if not chat_id:
                continue
            chats.append({
                "id": chat_id,
                "name": row.get("name") or row.get("contactName") or row.get("pushname") or "",
                "kind": "group" if chat_id.endswith("@g.us") else "direct",
            })
        return chats

    def settings(self) -> dict:
        return self._call("GET", "getSettings") or {}


def _parse_notification(body: dict, include_outgoing: bool = False) -> InboundMessage | None:
    hook = body.get("typeWebhook")
    accepted = {INCOMING_HOOK} | ({OUTGOING_PHONE_HOOK} if include_outgoing else set())
    if hook not in accepted:
        return None
    sender = body.get("senderData") or {}
    data = body.get("messageData") or {}
    chat_id = sender.get("chatId") or ""
    message_id = body.get("idMessage") or ""
    if not chat_id or not message_id:
        return None
    return _build(
        message_id=message_id,
        chat_id=chat_id,
        timestamp=int(body.get("timestamp") or 0),
        data=data,
        chat_name=sender.get("chatName") or "",
        sender=sender.get("sender") or "",
        sender_name=sender.get("senderName") or sender.get("senderContactName") or "",
        raw=body,
    )


def _parse_history_row(row: dict, chat_id: str, include_outgoing: bool = False) -> InboundMessage | None:
    """History rows are flatter than notifications and include outgoing messages.

    History cannot distinguish a phone-sent message from one this bot sent through the API.
    That is safe because the pipeline only ever acts on images and the bot only ever sends
    text, so its own replies can never be picked up as work.
    """
    if not isinstance(row, dict):
        return None
    direction = row.get("type") or "incoming"
    if direction != "incoming" and not (include_outgoing and direction == "outgoing"):
        return None
    message_id = row.get("idMessage") or ""
    if not message_id:
        return None
    type_message = row.get("typeMessage") or ""
    data: dict[str, Any] = {"typeMessage": type_message}
    if type_message in IMAGE_TYPES:
        data["fileMessageData"] = {
            "downloadUrl": row.get("downloadUrl") or "",
            "caption": row.get("caption") or "",
            "mimeType": row.get("mimeType") or "",
            "fileName": row.get("fileName") or "",
        }
    else:
        data["textMessageData"] = {"textMessage": row.get("textMessage") or row.get("extendedTextMessage", {}).get("text", "")}
    return _build(
        message_id=message_id,
        chat_id=row.get("chatId") or chat_id,
        timestamp=int(row.get("timestamp") or 0),
        data=data,
        chat_name=row.get("chatName") or "",
        sender=row.get("senderId") or "",
        sender_name=row.get("senderName") or "",
        raw=row,
    )


def _build(*, message_id: str, chat_id: str, timestamp: int, data: dict, chat_name: str,
           sender: str, sender_name: str, raw: dict) -> InboundMessage | None:
    type_message = data.get("typeMessage") or ""
    if type_message in IMAGE_TYPES:
        file_data = data.get("fileMessageData") or data.get("imageMessageData") or {}
        mime = file_data.get("mimeType") or ""
        # Green API reports stickers and some documents as images; only keep real images.
        if mime and not mime.startswith("image/"):
            kind = "other"
        else:
            kind = "image"
        return InboundMessage(
            id=message_id,
            chat_id=chat_id,
            kind=kind,
            timestamp=timestamp,
            text=(file_data.get("caption") or "").strip(),
            chat_name=chat_name,
            sender=sender,
            sender_name=sender_name,
            mime_type=mime,
            download_url=file_data.get("downloadUrl") or "",
            raw=raw,
        )
    if type_message in TEXT_TYPES:
        text_data = data.get("textMessageData") or data.get("extendedTextMessageData") or {}
        text = text_data.get("textMessage") or text_data.get("text") or ""
        return InboundMessage(
            id=message_id,
            chat_id=chat_id,
            kind="text",
            timestamp=timestamp,
            text=text.strip(),
            chat_name=chat_name,
            sender=sender,
            sender_name=sender_name,
            raw=raw,
        )
    return InboundMessage(
        id=message_id, chat_id=chat_id, kind="other", timestamp=timestamp,
        chat_name=chat_name, sender=sender, sender_name=sender_name, raw=raw,
    )


def _dedupe(messages: list[InboundMessage]) -> list[InboundMessage]:
    """Queue and history overlap by design; keep the first sighting of each id."""
    seen: set[str] = set()
    unique: list[InboundMessage] = []
    for message in messages:
        if message.id in seen:
            continue
        seen.add(message.id)
        unique.append(message)
    return sorted(unique, key=lambda m: m.timestamp)
