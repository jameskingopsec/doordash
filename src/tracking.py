"""Opaque authenticated tracking links used by every checkout surface."""

from __future__ import annotations

import base64
from functools import lru_cache
import os
from typing import Any
from urllib.parse import urlencode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


DEFAULT_TRACKER_BASE_URL = "https://succint-tracker.up.railway.app/"
SLUG_VERSION = "v1"
SLUG_AAD = b"ovio-dd-tracker-v1"

_tracker_base_url = DEFAULT_TRACKER_BASE_URL
_tracker_slug_secret = ""


def configure_tracking(base_url: str, slug_secret: str) -> None:
    """Configure the Relay domain and shared server-side AES key."""
    global _tracker_base_url, _tracker_slug_secret
    normalized_secret = (slug_secret or "").strip()
    if not normalized_secret:
        raise RuntimeError("tracking.slug_secret is required")
    _key_bytes(normalized_secret)
    _tracker_base_url = (base_url or DEFAULT_TRACKER_BASE_URL).rstrip("/") + "/"
    _tracker_slug_secret = normalized_secret


def _key_bytes(secret: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
    except (ValueError, TypeError) as exc:
        raise RuntimeError("tracking.slug_secret must be a base64url key") from exc
    if len(key) != 32:
        raise RuntimeError("tracking.slug_secret must decode to exactly 32 bytes")
    return key


@lru_cache(maxsize=4096)
def _encrypted_slug(order_id: str, secret: str) -> str:
    nonce = os.urandom(12)
    ciphertext = AESGCM(_key_bytes(secret)).encrypt(nonce, order_id.encode("utf-8"), SLUG_AAD)
    payload = base64.urlsafe_b64encode(nonce + ciphertext).rstrip(b"=").decode("ascii")
    return f"{SLUG_VERSION}.{payload}"


def encrypt_order_id(order_id: str, *, slug_secret: str | None = None) -> str:
    """Turn a private order ID into an authenticated, opaque Relay slug."""
    normalized = str(order_id or "").strip().lower()
    if not normalized:
        return ""
    secret = (slug_secret if slug_secret is not None else _tracker_slug_secret).strip()
    if not secret:
        return ""
    return _encrypted_slug(normalized, secret)


def order_tracker_url(
    job: dict[str, Any],
    *,
    slug_secret: str | None = None,
    base_url: str | None = None,
) -> str:
    order_id = str(job.get("order_uuid") or job.get("order_id") or "").strip()
    slug = encrypt_order_id(order_id, slug_secret=slug_secret)
    if not slug:
        return ""
    origin = (base_url or _tracker_base_url or DEFAULT_TRACKER_BASE_URL).rstrip("/") + "/"
    return f"{origin}?{urlencode({'order': slug})}"
