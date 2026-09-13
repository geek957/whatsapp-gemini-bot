"""Orchestration: read -> select -> Gemini -> reply -> record.

Selection rules (TRIGGER_MODE):
  any_image          every unseen image is analysed with the prompt file
  command            only images whose caption, or a nearby text message in the same
                     batch, starts with COMMAND_PREFIX
  command_or_caption like `command`, but an image carrying any caption also qualifies
                     and that caption becomes the question

Images that arrive together in one chat are answered in a single Gemini call and a single
reply, which keeps cost down and avoids spamming a group with one message per photo.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .gemini import GeminiBlocked, GeminiError, GeminiImage, GeminiResult, analyze
from .httpx import HttpError
from .providers.base import InboundMessage, Provider
from .state import State

LOG = logging.getLogger(__name__)


@dataclass
class Batch:
    chat_id: str
    chat_name: str
    images: list[InboundMessage] = field(default_factory=list)
    question: str = ""

    @property
    def reply_to(self) -> str:
        return self.images[-1].id if self.images else ""


@dataclass
class ChatOutcome:
    chat_id: str
    chat_name: str
    image_count: int
    status: str  # replied | dry_run | blocked | failed | skipped
    detail: str = ""
    reply_id: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0


@dataclass
class RunReport:
    read: int = 0
    images_seen: int = 0
    already_processed: int = 0
    not_triggered: int = 0
    outcomes: list[ChatOutcome] = field(default_factory=list)
    pruned: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def replies(self) -> int:
        return sum(1 for outcome in self.outcomes if outcome.status in {"replied", "dry_run"})

    @property
    def failed(self) -> bool:
        return bool(self.errors) or any(o.status == "failed" for o in self.outcomes)

    def to_dict(self) -> dict:
        return {
            "read": self.read,
            "images_seen": self.images_seen,
            "already_processed": self.already_processed,
            "not_triggered": self.not_triggered,
            "replies": self.replies,
            "pruned": self.pruned,
            "errors": self.errors,
            "chats": [
                {
                    "chat_id": o.chat_id,
                    "chat_name": o.chat_name,
                    "images": o.image_count,
                    "status": o.status,
                    "detail": o.detail,
                    "reply_id": o.reply_id,
                    "prompt_tokens": o.prompt_tokens,
                    "output_tokens": o.output_tokens,
                }
                for o in self.outcomes
            ],
        }

    def summary_markdown(self) -> str:
        lines = [
            "### WhatsApp -> Gemini run",
            "",
            f"- messages read: **{self.read}**",
            f"- images: **{self.images_seen}** (already answered: {self.already_processed}, not triggered: {self.not_triggered})",
            f"- replies sent: **{self.replies}**",
        ]
        if self.pruned:
            lines.append(f"- state entries pruned: {self.pruned}")
        if self.outcomes:
            lines += ["", "| chat | images | status | detail |", "| --- | --- | --- | --- |"]
            for o in self.outcomes:
                name = o.chat_name or o.chat_id
                lines.append(f"| {name} | {o.image_count} | {o.status} | {o.detail[:80] or '-'} |")
        if self.errors:
            lines += ["", "**Errors**", ""] + [f"- {error}" for error in self.errors]
        return "\n".join(lines) + "\n"


def load_prompt(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"prompt file not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt file is empty: {path}")
    return text


def run(cfg: Config, provider: Provider, state: State, *, now: int | None = None,
        analyzer=analyze) -> RunReport:
    """Execute one full cycle. Never raises for per-chat faults; collects them instead."""
    current = int(now if now is not None else time.time())
    report = RunReport()
    base_prompt = load_prompt(cfg.prompt_path)

    try:
        messages = provider.fetch_messages()
    except HttpError as exc:
        report.errors.append(f"fetch_messages failed: {exc}")
        return report
    report.read = len(messages)

    allowed = set(cfg.chat_ids)
    fresh = [m for m in messages if not allowed or m.chat_id in allowed]

    batches = _build_batches(cfg, fresh, state, report, current)

    for batch in batches[: cfg.max_replies_per_run]:
        report.outcomes.append(_handle_batch(cfg, provider, state, batch, base_prompt, analyzer, current))

    if len(batches) > cfg.max_replies_per_run:
        skipped = len(batches) - cfg.max_replies_per_run
        # Left unmarked in state on purpose: the next run picks them up.
        report.errors.append(f"{skipped} chat batch(es) deferred by MAX_REPLIES_PER_RUN")

    state.last_run_ts = current
    state.runs += 1
    report.pruned = state.prune(cfg.state_retention_days, cfg.state_max_ids, now=current)
    return report


def _build_batches(cfg: Config, messages: list[InboundMessage], state: State,
                   report: RunReport, now: int) -> list[Batch]:
    """Group unseen, triggered images per chat, oldest chat first."""
    window_start = now - cfg.window_minutes * 60
    by_chat: dict[str, Batch] = {}
    commands: dict[str, str] = {}

    for message in messages:
        if message.kind == "text":
            text = message.text
            if cfg.command_prefix and text.lower().startswith(cfg.command_prefix.lower()):  # command message
                commands[message.chat_id] = text[len(cfg.command_prefix):].strip()
        elif message.kind == "image":
            report.images_seen += 1

    for message in messages:
        if message.kind != "image":
            continue
        if state.seen(message.id):
            report.already_processed += 1
            continue
        if message.timestamp and message.timestamp < window_start:
            # Stale history rows: recorded so a later sweep stops reconsidering them.
            state.mark(message.id, now)
            report.not_triggered += 1
            continue
        question = _question_for(cfg, message, commands.get(message.chat_id, ""))
        if question is None:
            state.mark(message.id, now)
            report.not_triggered += 1
            continue
        batch = by_chat.setdefault(
            message.chat_id, Batch(chat_id=message.chat_id, chat_name=message.chat_name)
        )
        if len(batch.images) >= cfg.max_images_per_reply:
            continue  # left unmarked: picked up by the next run
        batch.images.append(message)
        if question and not batch.question:
            batch.question = question

    return [batch for batch in by_chat.values() if batch.images]


def _question_for(cfg: Config, message: InboundMessage, chat_command: str) -> str | None:
    """Return the user question, "" for the default prompt, or None to skip."""
    caption = message.text.strip()
    prefix = cfg.command_prefix.lower()
    starts_with_command = bool(prefix) and caption.lower().startswith(prefix)

    if cfg.trigger_mode == "any_image":
        if starts_with_command:
            return caption[len(cfg.command_prefix):].strip()
        return caption

    if starts_with_command:
        return caption[len(cfg.command_prefix):].strip()
    if chat_command:
        return chat_command
    if cfg.trigger_mode == "command_or_caption" and caption:
        return caption
    return None


def _handle_batch(cfg: Config, provider: Provider, state: State, batch: Batch,
                  base_prompt: str, analyzer, now: int) -> ChatOutcome:
    outcome = ChatOutcome(
        chat_id=batch.chat_id,
        chat_name=batch.chat_name,
        image_count=len(batch.images),
        status="failed",
    )
    images: list[GeminiImage] = []
    for message in batch.images:
        try:
            data, mime = provider.download_image(message)
        except HttpError as exc:
            LOG.error("download failed for %s: %s", message.id, exc)
            outcome.detail = f"download failed: {exc}"
            continue
        images.append(GeminiImage(mime_type=mime, data=data))

    if not images:
        outcome.status = "failed"
        outcome.detail = outcome.detail or "no images could be downloaded"
        return outcome

    prompt = _compose_prompt(base_prompt, batch)

    if cfg.dry_run:
        outcome.status = "dry_run"
        outcome.detail = f"would send {len(images)} image(s) with {len(prompt)}-char prompt"
        _mark_all(state, batch, now)
        return outcome

    try:
        result: GeminiResult = analyzer(cfg, prompt, images)
    except GeminiBlocked as exc:
        # Safety block is a final answer for this content: record it so the bot does not
        # retry the same images every five minutes.
        outcome.status = "blocked"
        outcome.detail = str(exc)
        _mark_all(state, batch, now)
        _try_send(provider, batch, cfg, "I can't analyse that image.", outcome)
        return outcome
    except GeminiError as exc:
        outcome.status = "failed"
        outcome.detail = str(exc)
        return outcome  # unmarked, so the next run retries

    outcome.prompt_tokens = result.prompt_tokens
    outcome.output_tokens = result.output_tokens
    reply_text = _format_reply(result.text, len(images))
    if not _try_send(provider, batch, cfg, reply_text, outcome):
        return outcome  # unmarked, so the next run retries

    outcome.status = "replied"
    outcome.detail = f"{len(reply_text)} chars"
    _mark_all(state, batch, now)
    return outcome


def _try_send(provider: Provider, batch: Batch, cfg: Config, text: str, outcome: ChatOutcome) -> bool:
    try:
        outcome.reply_id = provider.send_text(batch.chat_id, text, reply_to=batch.reply_to)
        return True
    except HttpError as exc:
        LOG.error("send failed for %s: %s", batch.chat_id, exc)
        outcome.status = "failed"
        outcome.detail = f"send failed: {exc}"
        return False


def _compose_prompt(base_prompt: str, batch: Batch) -> str:
    parts = [base_prompt]
    if len(batch.images) > 1:
        parts.append(f"You are given {len(batch.images)} images from the same conversation.")
    if batch.question:
        parts.append(f"The sender asked:\n{batch.question}")
    return "\n\n".join(parts)


def _format_reply(text: str, image_count: int) -> str:
    """WhatsApp rejects very long bodies; 4096 chars is the practical text limit."""
    limit = 4000
    body = text.strip()
    if len(body) > limit:
        body = body[: limit - 20].rstrip() + "\n\n[truncated]"
    return body


def _mark_all(state: State, batch: Batch, now: int) -> None:
    for message in batch.images:
        state.mark(message.id, now)
