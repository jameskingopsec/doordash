"""Compact public checkout-success webhook."""

from __future__ import annotations

from datetime import datetime, timezone
import random
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp

from .models import OrderSession, money


G6_SUCCESS = 0x10B981
CHECKOUT_PHRASES = (
    "just placed an order",
    "can't wait for their food!",
    "ordered and locked in",
    "just got their order in",
    "hungry and ready to order",
    "dropped an order",
    "ordered up",
    "food incoming",
    "ready for delivery",
    "just submitted their order",
)


def success_webhook_payload(
    session: OrderSession,
    job: dict[str, Any],
    bot_name: str,
    *,
    phrase: str | None = None,
) -> dict[str, Any]:
    """Render the small public Order Placed embed without private order fields."""
    cart = job.get("cart") or {}
    store = str(cart.get("store_name") or session.store_name or "Group order")[:100]
    total = money(cart.get("client_total_cents"))
    callout = phrase or random.choice(CHECKOUT_PHRASES)
    if callout[-1:] not in {".", "!", "?"}:
        callout += "!"
    return {
        "content": f"<@{session.user_id}> {callout}",
        "embeds": [{
            "title": "🎉 Order Placed",
            "description": f"🏪 **{store}**",
            "color": G6_SUCCESS,
            "fields": [
                {"name": "User", "value": f"<@{session.user_id}>", "inline": True},
                {"name": "Total", "value": total, "inline": True},
                {"name": "Store", "value": store, "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }],
        "allowed_mentions": {"users": [str(session.user_id)]},
    }


def webhook_url_with_wait(webhook_url: str) -> str:
    parts = urlsplit(webhook_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["wait"] = "true"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class SuccessNotifier:
    def __init__(self, webhook_url: str, bot_name: str, timeout_seconds: float = 15):
        self.webhook_url = webhook_url
        self.bot_name = bot_name
        self.timeout = aiohttp.ClientTimeout(total=timeout_seconds)

    async def send(self, session: OrderSession, job: dict[str, Any]) -> None:
        if not self.webhook_url:
            return
        payload = success_webhook_payload(session, job, self.bot_name)
        async with aiohttp.ClientSession(timeout=self.timeout) as client:
            async with client.post(webhook_url_with_wait(self.webhook_url), json=payload) as response:
                if not 200 <= response.status < 300:
                    detail = (await response.text())[:300]
                    raise RuntimeError(f"Success webhook returned HTTP {response.status}: {detail}")
