"""Gemini `generateContent` client for multimodal (text + image) prompts."""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from .config import Config
from .httpx import HttpError, request

LOG = logging.getLogger(__name__)

# Formats the Gemini vision models accept.
SUPPORTED_MIME = frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"})


class GeminiError(Exception):
    pass


class GeminiBlocked(GeminiError):
    """The prompt or response was stopped by safety filters, not by a transport fault."""


@dataclass(frozen=True)
class GeminiImage:
    mime_type: str
    data: bytes

    def normalised_mime(self) -> str:
        mime = (self.mime_type or "").split(";")[0].strip().lower()
        if mime == "image/jpg":
            return "image/jpeg"
        return mime


@dataclass(frozen=True)
class GeminiResult:
    text: str
    model: str
    finish_reason: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    truncated: bool = False


def analyze(cfg: Config, prompt: str, images: list[GeminiImage], http=request) -> GeminiResult:
    """Send one prompt plus inline images and return the model's text."""
    if not prompt.strip():
        raise GeminiError("prompt is empty")
    usable = [image for image in images if image.normalised_mime() in SUPPORTED_MIME]
    for image in images:
        if image.normalised_mime() not in SUPPORTED_MIME:
            LOG.warning("dropping unsupported image type %r", image.mime_type)
    if not usable:
        raise GeminiError("no images in a format Gemini accepts")

    parts: list[dict] = [{"text": prompt}]
    for image in usable:
        parts.append({
            "inline_data": {
                "mime_type": image.normalised_mime(),
                "data": base64.b64encode(image.data).decode("ascii"),
            }
        })

    generation_config: dict = {
        "temperature": cfg.gemini_temperature,
        "maxOutputTokens": cfg.gemini_max_output_tokens,
    }
    # Without this, a 2.5-class model spends most of maxOutputTokens on reasoning and the
    # reply arrives truncated mid-sentence. -1 means "let the model decide".
    if cfg.gemini_thinking_budget >= 0:
        generation_config["thinkingConfig"] = {"thinkingBudget": cfg.gemini_thinking_budget}
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": generation_config,
    }
    url = f"{cfg.gemini_api_base}/models/{cfg.gemini_model}:generateContent"
    try:
        payload = http(
            "POST", url,
            headers={"x-goog-api-key": cfg.gemini_api_key},
            json_body=body,
            timeout=max(cfg.http_timeout, 60),
            retries=cfg.http_retries,
        ).json() or {}
    except HttpError as exc:
        raise GeminiError(f"Gemini request failed: {exc}") from exc

    feedback = payload.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        raise GeminiBlocked(f"prompt blocked: {feedback['blockReason']}")

    candidates = payload.get("candidates") or []
    if not candidates:
        raise GeminiError("Gemini returned no candidates")
    candidate = candidates[0]
    finish = candidate.get("finishReason") or ""
    text = "".join(
        part.get("text", "")
        for part in (candidate.get("content") or {}).get("parts") or []
    ).strip()
    if not text:
        if finish in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST"}:
            raise GeminiBlocked(f"response blocked: {finish}")
        if finish == "MAX_TOKENS":
            raise GeminiError("response hit MAX_TOKENS before producing text; raise GEMINI_MAX_OUTPUT_TOKENS")
        raise GeminiError(f"Gemini returned empty text (finishReason={finish or 'unknown'})")

    usage = payload.get("usageMetadata") or {}
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    if finish == "MAX_TOKENS":
        # Text was produced but cut off. Surfacing this beats silently sending half a reply.
        LOG.warning(
            "response truncated: finishReason=MAX_TOKENS, %d reasoning tokens of a %d budget; "
            "raise GEMINI_MAX_OUTPUT_TOKENS or lower GEMINI_THINKING_BUDGET",
            thoughts, cfg.gemini_max_output_tokens,
        )
    return GeminiResult(
        text=text,
        model=cfg.gemini_model,
        finish_reason=finish,
        prompt_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
        thinking_tokens=thoughts,
        truncated=finish == "MAX_TOKENS",
    )


def list_models(cfg: Config, http=request) -> list[str]:
    """Model ids on this key that support generateContent, so the default can be checked."""
    payload = http(
        "GET", f"{cfg.gemini_api_base}/models",
        headers={"x-goog-api-key": cfg.gemini_api_key},
        timeout=cfg.http_timeout, retries=cfg.http_retries,
    ).json() or {}
    names = []
    for model in payload.get("models") or []:
        methods = model.get("supportedGenerationMethods") or []
        if "generateContent" in methods:
            names.append(str(model.get("name", "")).removeprefix("models/"))
    return sorted(name for name in names if name)
