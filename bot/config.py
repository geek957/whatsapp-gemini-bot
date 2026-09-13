"""Environment-driven configuration with aggregated validation."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

TRIGGER_MODES = ("any_image", "command", "command_or_caption")
READ_MODES = ("queue", "history", "both")
REPLY_MODES = ("quote", "plain")


class ConfigError(Exception):
    """Raised with every validation problem found, not just the first."""


def _split(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.replace("\n", ",").split(",") if part.strip())


def _int(name: str, raw: str, errors: list[str], minimum: int = 0) -> int:
    try:
        parsed = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer, got {raw!r}")
        return minimum
    if parsed < minimum:
        errors.append(f"{name} must be >= {minimum}, got {parsed}")
        return minimum
    return parsed


def _float(name: str, raw: str, errors: list[str]) -> float:
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name} must be a number, got {raw!r}")
        return 0.0


def _choice(name: str, raw: str, allowed: tuple[str, ...], errors: list[str]) -> str:
    if raw not in allowed:
        errors.append(f"{name} must be one of {'|'.join(allowed)}, got {raw!r}")
        return allowed[0]
    return raw


@dataclass(frozen=True)
class Config:
    provider: str = "greenapi"

    green_instance_id: str = ""
    green_token: str = ""
    green_api_base: str = "https://api.green-api.com"
    green_media_base: str = "https://media.green-api.com"

    meta_phone_number_id: str = ""
    meta_access_token: str = ""
    meta_api_base: str = "https://graph.facebook.com/v21.0"

    chat_ids: tuple[str, ...] = ()

    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_base: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_temperature: float = 0.4
    # A cap, not a reservation: billing follows tokens actually generated, so this is
    # set high and WhatsApp message chunking is the real limit on reply length.
    gemini_max_output_tokens: int = 32768
    # Gemini 2.5+ are thinking models and charge reasoning tokens against
    # maxOutputTokens, which silently truncates the visible answer. 0 disables thinking
    # (flash only), -1 lets the model decide, a positive value caps it.
    gemini_thinking_budget: int = 0

    prompt_path: Path = Path("prompts/default.md")
    trigger_mode: str = "command_or_caption"
    command_prefix: str = "/ask"
    read_mode: str = "both"
    # Process images you post yourself from the linked phone. Required when the bot number
    # and the person posting are the same account, as in a personal automation group.
    include_outgoing: bool = False
    history_count: int = 40
    window_minutes: int = 60
    max_images_per_reply: int = 6
    max_image_bytes: int = 7 * 1024 * 1024
    max_replies_per_run: int = 5
    # WhatsApp rejects very long bodies; longer answers are split across messages.
    max_reply_chars: int = 4000
    max_reply_parts: int = 12

    state_path: Path = Path(".state/state.json")
    state_retention_days: int = 14
    state_max_ids: int = 5000

    dry_run: bool = False
    reply_mode: str = "quote"
    http_timeout: int = 30
    http_retries: int = 3
    log_level: str = "INFO"

    errors: tuple[str, ...] = field(default=(), repr=False)

    @property
    def requires_gemini(self) -> bool:
        return not self.dry_run

    def raise_for_errors(self) -> None:
        if self.errors:
            raise ConfigError("Invalid configuration:\n- " + "\n- ".join(self.errors))


def from_env(env: dict[str, str] | None = None) -> Config:
    """Build a Config from environment variables, collecting all problems."""
    src = dict(os.environ if env is None else env)
    errors: list[str] = []

    def get(name: str, default: str = "") -> str:
        """Empty or unset falls back to the default; used where "" is meaningless."""
        return (src.get(name) or default).strip()

    def get_exact(name: str, default: str = "") -> str:
        """Respects an explicitly empty value, so a prefix can be deliberately disabled."""
        value = src.get(name)
        return default.strip() if value is None else value.strip()

    provider = _choice("PROVIDER", get("PROVIDER", "greenapi").lower(), ("greenapi", "meta"), errors)
    dry_run = get("DRY_RUN", "false").lower() in {"1", "true", "yes", "on"}

    if provider == "greenapi":
        if not get("GREEN_API_INSTANCE_ID"):
            errors.append("GREEN_API_INSTANCE_ID is required for PROVIDER=greenapi")
        if not get("GREEN_API_TOKEN"):
            errors.append("GREEN_API_TOKEN is required for PROVIDER=greenapi")
    else:
        if not get("META_PHONE_NUMBER_ID"):
            errors.append("META_PHONE_NUMBER_ID is required for PROVIDER=meta")
        if not get("META_ACCESS_TOKEN"):
            errors.append("META_ACCESS_TOKEN is required for PROVIDER=meta")

    if not get("GEMINI_API_KEY") and not dry_run:
        errors.append("GEMINI_API_KEY is required (or set DRY_RUN=true)")

    trigger_mode = _choice("TRIGGER_MODE", get("TRIGGER_MODE", "command_or_caption").lower(), TRIGGER_MODES, errors)
    read_mode = _choice("READ_MODE", get("READ_MODE", "both").lower(), READ_MODES, errors)
    reply_mode = _choice("REPLY_MODE", get("REPLY_MODE", "quote").lower(), REPLY_MODES, errors)

    command_prefix = get_exact("COMMAND_PREFIX", "/ask")
    if trigger_mode in {"command", "command_or_caption"} and not command_prefix:
        errors.append(f"COMMAND_PREFIX must not be empty when TRIGGER_MODE={trigger_mode}")

    prompt_path = Path(get("PROMPT_PATH", "prompts/default.md"))

    cfg = Config(
        provider=provider,
        green_instance_id=get("GREEN_API_INSTANCE_ID"),
        green_token=get("GREEN_API_TOKEN"),
        green_api_base=get("GREEN_API_BASE", "https://api.green-api.com").rstrip("/"),
        green_media_base=get("GREEN_API_MEDIA_BASE", "https://media.green-api.com").rstrip("/"),
        meta_phone_number_id=get("META_PHONE_NUMBER_ID"),
        meta_access_token=get("META_ACCESS_TOKEN"),
        meta_api_base=get("META_API_BASE", "https://graph.facebook.com/v21.0").rstrip("/"),
        chat_ids=_split(get("WHATSAPP_CHAT_IDS")),
        gemini_api_key=get("GEMINI_API_KEY"),
        gemini_model=get("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_api_base=get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").rstrip("/"),
        gemini_temperature=_float("GEMINI_TEMPERATURE", get("GEMINI_TEMPERATURE", "0.4"), errors),
        gemini_max_output_tokens=_int("GEMINI_MAX_OUTPUT_TOKENS", get("GEMINI_MAX_OUTPUT_TOKENS", "32768"), errors, 1),
        gemini_thinking_budget=_int("GEMINI_THINKING_BUDGET", get("GEMINI_THINKING_BUDGET", "0"), errors, -1),
        prompt_path=prompt_path,
        trigger_mode=trigger_mode,
        command_prefix=command_prefix,
        read_mode=read_mode,
        include_outgoing=get("INCLUDE_OUTGOING", "false").lower() in {"1", "true", "yes", "on"},
        history_count=_int("HISTORY_COUNT", get("HISTORY_COUNT", "40"), errors, 1),
        window_minutes=_int("WINDOW_MINUTES", get("WINDOW_MINUTES", "60"), errors, 1),
        max_images_per_reply=_int("MAX_IMAGES_PER_REPLY", get("MAX_IMAGES_PER_REPLY", "6"), errors, 1),
        max_image_bytes=_int("MAX_IMAGE_BYTES", get("MAX_IMAGE_BYTES", str(7 * 1024 * 1024)), errors, 1024),
        max_replies_per_run=_int("MAX_REPLIES_PER_RUN", get("MAX_REPLIES_PER_RUN", "5"), errors, 1),
        max_reply_chars=_int("MAX_REPLY_CHARS", get("MAX_REPLY_CHARS", "4000"), errors, 500),
        max_reply_parts=_int("MAX_REPLY_PARTS", get("MAX_REPLY_PARTS", "12"), errors, 1),
        state_path=Path(get("STATE_PATH", ".state/state.json")),
        state_retention_days=_int("STATE_RETENTION_DAYS", get("STATE_RETENTION_DAYS", "14"), errors, 1),
        state_max_ids=_int("STATE_MAX_IDS", get("STATE_MAX_IDS", "5000"), errors, 100),
        dry_run=dry_run,
        reply_mode=reply_mode,
        http_timeout=_int("HTTP_TIMEOUT", get("HTTP_TIMEOUT", "30"), errors, 1),
        http_retries=_int("HTTP_RETRIES", get("HTTP_RETRIES", "3"), errors, 1),
        log_level=get("LOG_LEVEL", "INFO").upper(),
        errors=tuple(errors),
    )
    return cfg
