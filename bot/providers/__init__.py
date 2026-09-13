"""Provider registry."""

from __future__ import annotations

from ..config import Config
from .base import InboundMessage, Provider
from .greenapi import GreenApiProvider
from .meta import MetaCloudProvider

__all__ = ["InboundMessage", "Provider", "GreenApiProvider", "MetaCloudProvider", "build_provider"]


def build_provider(cfg: Config) -> Provider:
    if cfg.provider == "greenapi":
        return GreenApiProvider(cfg)
    if cfg.provider == "meta":
        return MetaCloudProvider(cfg)
    raise ValueError(f"unknown provider {cfg.provider!r}")
