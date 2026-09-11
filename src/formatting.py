"""Discord-safe text renderers for Woolix jobs."""

from __future__ import annotations

import json
from typing import Any

from .models import OrderSession, money, mask_card


def truncate(value: Any, limit: int = 300) -> str:
    text = str(value or "—")
    return text if len(text) <= limit else f"{text[:limit - 1]}…"


def customer_block(session: OrderSession) -> str:
    handoff = "Delivery" if session.fulfillment == "delivery" else "Pickup"
    lines = []
    if session.name:
        lines.append(f"👤 **{truncate(session.name, 80)}**")
    lines.append(f"🏪 **{handoff}**")
    if session.address:
        address = session.address + (f", {session.unit}" if session.unit else "")
        lines.append(f"📍 {truncate(address, 220)}")
    if session.delivery_note:
        lines.append(f"📝 _{truncate(session.delivery_note, 180)}_")
    return "\n".join(lines)


def item_lines(job: dict[str, Any], limit: int = 12) -> str:
    items = (job.get("cart") or {}).get("items") or []
    if not items:
        return "_No items returned._"
    rendered = []
    for item in items[:limit]:
        quantity = int(item.get("quantity") or 1)
        rendered.append(
            f"**{quantity}×** {truncate(item.get('name') or 'Item', 90)} — "
            f"{money(int(item.get('unit_price_cents') or 0) * quantity)}"
        )
    if len(items) > limit:
        rendered.append(f"-# …and {len(items) - limit} more item(s)")
    return "\n".join(rendered)


def suspected_duplicate_item_cents(job: dict[str, Any]) -> int | None:
    """Detect a hidden duplicate when the subtotal contains one extra visible item.

    Some items have unlisted modifier pricing, so an arbitrary subtotal mismatch
    is not enough. This only flags the precise high-confidence pattern where the
    unexplained amount equals a displayed item's unit or extended price.
    """
    cart = job.get("cart") or {}
    items = cart.get("items") or []
    if not isinstance(items, list) or not items:
        return None
    try:
        subtotal = int(cart.get("subtotal_cents"))
    except (TypeError, ValueError):
        return None

    visible_total = 0
    duplicate_candidates: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            return None
        try:
            quantity = max(1, int(item.get("quantity") or 1))
            unit_price = int(item.get("unit_price_cents") or 0)
        except (TypeError, ValueError):
            return None
        if unit_price <= 0:
            return None
        extended = unit_price * quantity
        visible_total += extended
        duplicate_candidates.update((unit_price, extended))

    unexplained = subtotal - visible_total
    return unexplained if unexplained > 0 and unexplained in duplicate_candidates else None


def price_lines(job: dict[str, Any]) -> str:
    cart = job.get("cart") or {}
    return (
        f"Subtotal: **{money(cart.get('subtotal_cents'))}**\n"
        f"Promotion: **-{money(cart.get('promo_discount_cents'))}**\n"
        f"Tax & fees: **{money(cart.get('tax_and_fees_cents'))}**\n"
        f"Tip: **{money((job.get('input') or {}).get('tip'))}**\n"
        f"### Total: {money(cart.get('client_total_cents'))}"
    )


def payment_line(session: OrderSession) -> str:
    return f"💳 **Payment:** `{mask_card(session.card_last4)}`"


def _error_context(value: Any) -> tuple[str, int, str, str]:
    status = int(getattr(value, "status", 0) or 0)
    payload = getattr(value, "payload", value)
    kind = ""
    payment_status = ""
    if isinstance(payload, dict):
        if not status:
            try:
                status = int(payload.get("http_status") or payload.get("status_code") or 0)
            except (TypeError, ValueError):
                status = 0
        payment_status = str(payload.get("payment_status") or "").strip().lower()
        for key in ("card_error", "place_order_error", "error"):
            if payload.get(key):
                kind = key
                break
    try:
        text = json.dumps(payload, default=str).lower()
    except (TypeError, ValueError):
        text = str(payload).lower()
    return kind, status, payment_status, text


def friendly_error_details(value: Any) -> tuple[str, str]:
    """Turn every service/job failure into safe, actionable customer language."""
    kind, status, payment_status, text = _error_context(value)

    def has(*clues: str) -> bool:
        return any(clue in text for clue in clues)

    if has("invalid_cvv", "incorrect_cvc", "incorrect cvv", "security code", "cvv mismatch", "cvc_check_failed"):
        return "Check Your Card", "The card security code is incorrect. Check the CVV and try again."
    if has("avs_mismatch", "incorrect_zip", "billing zip", "postal code mismatch", "postal_code_mismatch"):
        return "Check Your Card", "The billing ZIP does not match the card. Check it and try again."
    if has("expired_card", "card expired", "expired card"):
        return "Card Expired", "This card is expired. Add a different card to continue."
    if has("incorrect_number", "invalid card number", "invalid_card_number"):
        return "Check Your Card", "The card number is invalid. Check it and try again."
    if has("insufficient_balance", "insufficient_funds", "insufficient funds"):
        return "Payment Declined", "This card has insufficient funds. Add a different card to continue."
    if has("card_not_supported", "unsupported card", "card type not supported"):
        return "Card Not Supported", "This card cannot be used for this payment. Add a different card to continue."
    if has("transaction_not_allowed", "restricted_card", "restricted card", "transaction not permitted"):
        return "Payment Not Allowed", "The card issuer did not allow this payment. Add a different card or contact the issuer."
    if has("lost_card", "stolen_card", "pickup_card", "card reported lost", "card reported stolen"):
        return "Card Unavailable", "The card issuer blocked this card. Use a different card or contact the issuer."
    if has("fraudulent", "suspected_fraud", "fraud suspected", "security block"):
        return "Payment Blocked", "The card issuer blocked this payment for security. Use a different card or contact the issuer."
    if has("velocity_exceeded", "too many payment attempts"):
        return "Too Many Attempts", "The card issuer blocked repeated attempts. Wait before retrying or use a different card."
    if has("duplicate_transaction", "duplicate transaction", "duplicate payment"):
        return "Duplicate Payment Blocked", "The card issuer detected a duplicate attempt. Wait a moment before trying again."
    if has("issuer_unavailable", "processing_error", "processor unavailable", "payment processor unavailable"):
        return "Payment Processing Error", "The card issuer could not process the payment right now. Wait a moment or use a different card."
    if has("do_not_honor", "do not honor", "generic_decline", "issuer_declined"):
        return "Payment Declined", "The card issuer declined this payment. Add a different card or contact the issuer."
    if has("card_declined", "card declined", "card was declined", "payment declined", "payment_declined"):
        return "Card Declined", "Your card was declined. Add a different card or contact your card issuer."
    if kind == "card_error" or has("payment service", "invalid card", "payment method"):
        return "Check Your Card", "We could not verify this card. Check the details or add a different card."

    if has("already used", "cart used", "cart_reused", "duplicate cart"):
        return "Fresh Cart Needed", "This group cart link was already used. Create a fresh group cart and try again."
    if has("invalid_group", "invalid group", "cart not found", "group cart not found", "bad cart link"):
        return "Check Your Cart Link", "This group cart link is invalid or expired. Create a fresh group cart and try again."
    if has("empty cart", "cart is empty", "no items"):
        return "Cart Is Empty", "This group cart has no items. Add at least one item and try again."
    if has("out of stock", "item unavailable", "items unavailable", "sold out"):
        return "Update Your Cart", "One or more items are unavailable. Remove or replace them, then try again."
    if has("store closed", "merchant closed", "store unavailable", "merchant unavailable", "not accepting orders"):
        return "Store Unavailable", "This store is closed or unavailable right now. Choose another store and try again."
    if has(
        "invalid address", "invalid_address", "address not found", "address_not_found",
        "not deliverable", "not_deliverable", "outside delivery", "delivery area", "too far",
    ):
        return "Check Your Address", "The delivery address could not be confirmed. Check the full address or use another one."
    if has("promo invalid", "promotion invalid", "promo expired", "promotion expired"):
        return "Discount Unavailable", "That discount is no longer available. Create a fresh cart and try again."

    if payment_status == "declined":
        return "Payment Declined", "The card issuer declined this payment. Add a different card or contact the issuer."
    if payment_status == "failed":
        return "Payment Failed", "The payment could not be completed. Check the card details or add a different card."

    if has("draft expired", "job expired", '"status": "expired"'):
        return "Draft Expired", "This checkout draft expired. Send `!start` to create a new one."
    if has('"status": "cancelled"', "order cancelled", "job cancelled"):
        return "Order Cancelled", "This order was cancelled. Send `!start` when you are ready to try again."
    if status == 404 or has("order not found", "job not found"):
        return "Order No Longer Available", "This order is no longer available. Send `!start` to create a new one."
    if status == 409 or has("already processing", "already placed", "conflict"):
        return "Order Already Updated", "This order changed or is already processing. Send `!start` to reopen it."
    if status == 429 or has("rate limit", "too many requests"):
        return "Checkout Is Busy", "Too many orders are starting right now. Wait a moment and try again."
    if status in {401, 403}:
        return "Checkout Unavailable", "Checkout is temporarily unavailable. Please contact the developer."
    if status == 0 and has("timeout", "timed out", "could not reach", "connection"):
        return "Checkout Is Taking Longer", "Wait a moment, then send `!start` to reopen your order."
    if status >= 500:
        return "Checkout Temporarily Unavailable", "Wait a moment and try again. If it continues, contact the developer."
    if status in {400, 422}:
        return "Check Your Order Details", "Check the group cart link and full delivery address, then try again."
    if kind == "place_order_error":
        return "Order Was Not Placed", "Review the cart and payment details, then try again. Nothing was charged."
    return (
        "Checkout Needs Attention",
        "We could not finish this checkout. Try again with a fresh group cart link, or contact the developer if it continues.",
    )


def error_message(job: dict[str, Any]) -> str:
    return friendly_error_details(job)[1]
