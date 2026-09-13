"""Minimal JSON/binary HTTP client on the standard library.

Deliberately dependency-free: the workflow runs with no `pip install` step, which
removes a whole class of CI failures (registry outages, lockfile drift, supply chain).
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

LOG = logging.getLogger(__name__)

RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: bytes):
        self.status = status
        self.url = url
        self.body = body
        snippet = body[:500].decode("utf-8", "replace")
        super().__init__(f"HTTP {status} for {url}: {snippet}")


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]

    def json(self) -> Any:
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except json.JSONDecodeError as exc:
            raise HttpError(self.status, "<decode>", self.body) from exc


def request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    json_body: Any = None,
    timeout: int = 30,
    retries: int = 3,
    sleep=time.sleep,
) -> Response:
    """Perform one request, retrying transient failures with jittered backoff."""
    data = None
    all_headers = {"Accept": "application/json", "User-Agent": "whatsapp-gemini-bot/1.0"}
    if headers:
        all_headers.update(headers)
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        all_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(url, data=data, headers=all_headers, method=method.upper())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(resp.status, resp.read(), dict(resp.headers.items()))
        except urllib.error.HTTPError as exc:  # 4xx/5xx
            body = exc.read()
            if exc.code in RETRY_STATUSES and attempt < retries:
                last_error = HttpError(exc.code, url, body)
                delay = _backoff(attempt, exc.headers.get("Retry-After"))
                LOG.warning("%s %s -> %s, retrying in %.1fs (%d/%d)", method, _safe(url), exc.code, delay, attempt, retries)
                sleep(delay)
                continue
            raise HttpError(exc.code, url, body) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt >= retries:
                break
            delay = _backoff(attempt, None)
            LOG.warning("%s %s -> %s, retrying in %.1fs (%d/%d)", method, _safe(url), exc, delay, attempt, retries)
            sleep(delay)

    raise HttpError(0, url, str(last_error or "request failed").encode())


def get_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = 30, retries: int = 3,
              max_bytes: int | None = None, sleep=time.sleep) -> tuple[bytes, str]:
    """Download binary content, returning (payload, content-type)."""
    resp = request("GET", url, headers=headers, timeout=timeout, retries=retries, sleep=sleep)
    if max_bytes is not None and len(resp.body) > max_bytes:
        raise HttpError(413, url, f"payload {len(resp.body)} exceeds limit {max_bytes}".encode())
    content_type = resp.headers.get("Content-Type", "application/octet-stream").split(";")[0].strip()
    return resp.body, content_type


def _backoff(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            return min(float(retry_after), 30.0)
        except ValueError:
            pass
    return min(2.0 ** (attempt - 1), 8.0) + random.uniform(0, 0.4)


def _safe(url: str) -> str:
    """Green API embeds the instance token in the path; never log it."""
    parts = url.split("/")
    return "/".join(part if len(part) < 24 else part[:6] + "***" for part in parts)
