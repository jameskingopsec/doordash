"""Small GetAText balance client used by the private user dashboard."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp


BALANCE_URL = "https://getatext.com/api/v1/balance"


class GetATextError(RuntimeError):
    """A customer-safe GetAText connection error."""


async def getatext_balance(api_key: str) -> Decimal:
    """Return the account balance without exposing provider response details."""
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {"Auth": api_key, "Accept": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(BALANCE_URL, headers=headers) as response:
                if response.status in {401, 403}:
                    raise GetATextError("That key was not accepted. Update it and try again.")
                if response.status == 429:
                    raise GetATextError("Balance is temporarily unavailable. Refresh in a moment.")
                if not 200 <= response.status < 300:
                    raise GetATextError("Balance is temporarily unavailable.")
                try:
                    payload: Any = await response.json(content_type=None)
                except (ValueError, TypeError) as exc:
                    raise GetATextError("Balance is temporarily unavailable.") from exc
    except GetATextError:
        raise
    except (aiohttp.ClientError, TimeoutError) as exc:
        raise GetATextError("Balance is temporarily unavailable.") from exc

    raw = payload.get("balance") if isinstance(payload, dict) else payload
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise GetATextError("Balance is temporarily unavailable.") from exc
