"""Durable dedupe state.

Every answered message id is recorded so a redelivered notification, an overlapping
history sweep, or a re-run of the same workflow never produces a second reply. The file
is committed to a dedicated git branch by the workflow, which is the only storage on
GitHub that survives indefinitely (caches are evicted, artifacts expire).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger(__name__)

SCHEMA_VERSION = 1


@dataclass
class State:
    processed: dict[str, int] = field(default_factory=dict)  # message id -> unix ts answered
    last_run_ts: int = 0
    runs: int = 0

    # ------------------------------------------------------------------ queries

    def seen(self, message_id: str) -> bool:
        return message_id in self.processed

    def mark(self, message_id: str, when: int | None = None) -> None:
        self.processed[message_id] = int(when if when is not None else time.time())

    # ------------------------------------------------------------- persistence

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.is_file():
            LOG.info("no state at %s; starting empty", path)
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt state file must not wedge the bot; worst case is one duplicate reply.
            LOG.error("unreadable state at %s (%s); starting empty", path, exc)
            return cls()
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> State:
        if not isinstance(raw, dict):
            return cls()
        processed_raw = raw.get("processed") or {}
        processed: dict[str, int] = {}
        if isinstance(processed_raw, dict):
            for key, value in processed_raw.items():
                try:
                    processed[str(key)] = int(value)
                except (TypeError, ValueError):
                    processed[str(key)] = 0
        elif isinstance(processed_raw, list):  # tolerate an older list-of-ids layout
            processed = {str(key): 0 for key in processed_raw}
        return cls(
            processed=processed,
            last_run_ts=int(raw.get("last_run_ts") or 0),
            runs=int(raw.get("runs") or 0),
        )

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "last_run_ts": self.last_run_ts,
            "runs": self.runs,
            "processed": dict(sorted(self.processed.items(), key=lambda kv: (kv[1], kv[0]))),
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # atomic: a killed run never leaves a half-written state

    # ------------------------------------------------------------------ upkeep

    def prune(self, retention_days: int, max_ids: int, now: int | None = None) -> int:
        """Drop entries older than the retention window, then cap by count."""
        current = int(now if now is not None else time.time())
        cutoff = current - retention_days * 86400
        before = len(self.processed)
        kept = {key: ts for key, ts in self.processed.items() if ts >= cutoff}
        if len(kept) > max_ids:
            newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:max_ids]
            kept = dict(newest)
        self.processed = kept
        return before - len(kept)

    def merge(self, other: State) -> State:
        """Union of both histories; used when a concurrent run already pushed state."""
        merged = dict(other.processed)
        for key, ts in self.processed.items():
            merged[key] = max(ts, merged.get(key, 0))
        return State(
            processed=merged,
            last_run_ts=max(self.last_run_ts, other.last_run_ts),
            runs=max(self.runs, other.runs),
        )
