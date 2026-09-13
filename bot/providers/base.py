"""Transport-neutral message model and provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class InboundMessage:
    """One inbound WhatsApp message, normalised across providers."""

    id: str
    chat_id: str
    kind: str  # "image" | "text" | "other"
    timestamp: int = 0
    text: str = ""  # caption for images, body for text
    chat_name: str = ""
    sender: str = ""
    sender_name: str = ""
    mime_type: str = ""
    download_url: str = ""
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_group(self) -> bool:
        return self.chat_id.endswith("@g.us")


class Provider(ABC):
    """A WhatsApp transport that can read inbound messages and publish replies."""

    name: str = "provider"

    @abstractmethod
    def fetch_messages(self) -> list[InboundMessage]:
        """Return inbound messages, newest-last. Must not raise on an empty inbox."""

    @abstractmethod
    def download_image(self, message: InboundMessage) -> tuple[bytes, str]:
        """Return (image_bytes, mime_type) for an image message."""

    @abstractmethod
    def send_text(self, chat_id: str, text: str, reply_to: str | None = None) -> str:
        """Publish a text message, returning the provider message id."""

    def ack(self, message: InboundMessage) -> None:
        """Acknowledge consumption where the transport uses a queue. No-op by default."""
