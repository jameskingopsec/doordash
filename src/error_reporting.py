"""Sanitized diagnostic rendering for the private Discord log channel."""

from __future__ import annotations

import json
import re
import traceback
from typing import Any


_SECRET_KEYS = re.compile(
    r"(?:authorization|api[_-]?key|token|password|secret|cvv|cvc|webhook)",
    re.I,
)
_CARD_NUMBER = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
_DISCORD_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}")
_API_KEY = re.compile(r"dda_[A-Za-z0-9_-]+", re.I)
_WEBHOOK = re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\d+/[^\s\"']+", re.I)


def redact_text(value: str) -> str:
    text = _WEBHOOK.sub("[WEBHOOK REDACTED]", value)
    text = _DISCORD_TOKEN.sub("[TOKEN REDACTED]", text)
    text = _API_KEY.sub("[API KEY REDACTED]", text)
    return _CARD_NUMBER.sub("[CARD REDACTED]", text)


def _sanitize(value: Any, key: str = "") -> Any:
    lowered_key = key.lower()
    if key and (
        _SECRET_KEYS.search(key)
        or lowered_key in {"card", "card_number", "payment_card"}
        or lowered_key.endswith("_card_number")
    ):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(part): _sanitize(item, str(part)) for part, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if isinstance(value, bytes):
        return f"[{len(value)} bytes]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_text(repr(value))


def diagnostic_dump(error: Any) -> str:
    """Return detailed diagnostics with credentials and payment data removed."""
    payload = getattr(error, "payload", None)
    details = {
        "type": type(error).__name__,
        "status": getattr(error, "status", None),
        "code": getattr(error, "code", None),
        "message": str(error),
        "payload": payload,
    }
    if isinstance(error, dict):
        details = {"type": "job_error", "payload": error}
    rendered = json.dumps(_sanitize(details), indent=2, ensure_ascii=False, default=str)
    exception_traceback = getattr(error, "__traceback__", None)
    if exception_traceback is not None:
        frames = "".join(traceback.format_list(traceback.extract_tb(exception_traceback)))
        rendered += f"\n\nTRACEBACK\n{redact_text(frames)}"
    return rendered


def event_log_message(
    event: str,
    *,
    level: str = "info",
    user_id: int | None = None,
    job_id: str = "",
    details: dict[str, Any] | None = None,
    timestamp: str = "",
) -> str:
    """Render a compact operational log with the same secret redaction as errors."""
    normalized_level = str(level or "info").strip().lower()
    icons = {
        "info": "🟦",
        "success": "🟢",
        "warning": "🟡",
        "admin": "🟣",
    }
    lines = [
        f"{icons.get(normalized_level, '⬜')} **OVIO DD Activity**",
        f"**Event:** {redact_text(str(event or 'Activity'))[:120]}",
        f"**Level:** `{normalized_level.upper()[:20]}`",
        f"**User ID:** `{user_id or 'system'}`",
        f"**Job ID:** `{redact_text(str(job_id or 'not assigned'))[:120]}`",
    ]
    if timestamp:
        lines.append(f"**Time:** `{redact_text(timestamp)[:80]}`")

    safe_details = _sanitize(details or {})
    if isinstance(safe_details, dict) and safe_details:
        lines.append("**Details:**")
        for key, value in list(safe_details.items())[:12]:
            safe_key = redact_text(str(key)).replace("\n", " ")[:60]
            if isinstance(value, (dict, list)):
                rendered = json.dumps(value, ensure_ascii=False, default=str)
            else:
                rendered = str(value)
            if not (safe_key.lower().endswith("_id") and isinstance(value, int)):
                rendered = redact_text(rendered)
            rendered = rendered.replace("\n", " ")[:240]
            lines.append(f"• **{safe_key}:** `{rendered}`")
    return "\n".join(lines)[:1900]
