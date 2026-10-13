"""HTTP helper with timeouts, bounded exponential backoff and secret redaction."""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

USER_AGENT = "job-alert-bot/1.0 (personal job alert; +https://github.com/suryasrir/job-alert-bot)"
DEFAULT_TIMEOUT = (10, 30)  # connect, read
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Environment variables whose values must never appear in logs or errors.
SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "OPENAI_API_KEY",
    "ADZUNA_APP_ID",
    "ADZUNA_APP_KEY",
    "SMTP_PASSWORD",
    "SMTP_USER",
    "CANDIDATE_PROFILE_JSON",
)


class FetchError(Exception):
    """A request failed after retries. The message is already redacted."""


def redact(text: str) -> str:
    """Replace any configured secret values in `text` with ***."""
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 4:
            text = text.replace(value, "***")
    return text


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


_SESSION: Optional[requests.Session] = None


def session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = _session()
    return _SESSION


def request(
    method: str,
    url: str,
    *,
    retries: int = 3,
    backoff: float = 1.5,
    max_sleep: float = 30.0,
    timeout=DEFAULT_TIMEOUT,
    **kwargs: Any,
) -> requests.Response:
    """Perform a request, retrying transient failures with bounded backoff.

    Raises FetchError (with secrets redacted) when all attempts fail or the
    server returns a non-retryable error status.
    """
    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            resp = session().request(method, url, timeout=timeout, **kwargs)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}"
        else:
            if resp.status_code < 400:
                return resp
            last_error = f"HTTP {resp.status_code}"
            if resp.status_code not in RETRY_STATUSES:
                break
            retry_after = resp.headers.get("Retry-After", "")
            if retry_after.isdigit():
                if attempt < retries:
                    time.sleep(min(float(retry_after), max_sleep))
                continue
        if attempt < retries:
            time.sleep(min(backoff * (2**attempt), max_sleep))
    # Strip query strings (may contain API keys) before reporting the URL.
    safe_url = url.split("?", 1)[0]
    raise FetchError(redact(f"{method} {safe_url} failed: {last_error}"))


def get_json(url: str, **kwargs: Any) -> Any:
    resp = request("GET", url, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(f"GET {url.split('?', 1)[0]} returned malformed JSON") from exc
