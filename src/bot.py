"""Discord interaction flow for Woolix priced drafts and real order placement."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .addressing import normalize_address
from .api import WoolixApiClient, WoolixApiError
from .billing import BillingStore, CHECKOUT_FEE_CENTS
from .config import DATA_DIR, Settings
from .error_reporting import diagnostic_dump, event_log_message
from .formatting import (
    customer_block,
    error_message,
    friendly_error_details,
    item_lines,
    payment_line,
    price_lines,
    suspected_duplicate_item_cents,
    truncate,
)
from .getatext import GetATextError, getatext_balance
from .models import FlowState, OrderSession, SETTLED_PAYMENT_STATUSES, money, parse_card
from .notifications import SuccessNotifier
from .store import SessionStore
from .tracking import configure_tracking, order_tracker_url
from .ui import C, E, Panel, button, link_button, load_custom_emojis, notice, sync_app_emojis
from .user_settings import UserSettingsStore, mask_secret


logger = logging.getLogger(__name__)
SETUP_CONNECTION_ERROR = "Could not establish a checkout connection. Contact the developer."
SETUP_ERROR_DELAY_SECONDS = 2.0
SUCCESS_MONITOR_MAX_INTERVAL_SECONDS = 2.0


def is_credit_exhausted(value: Any) -> bool:
    """Recognize depleted API-credit responses without exposing the provider."""
    status = value.status if isinstance(value, WoolixApiError) else 0
    payload = value.payload if isinstance(value, WoolixApiError) else value
    try:
        text = json.dumps(payload, default=str).lower()
    except TypeError:
        text = str(payload).lower()
    if status == 402:
        return True
    codes = ("insufficient_credit", "credits_exhausted", "credit_exhausted", "quota_exceeded")
    if any(code in text for code in codes):
        return True
    return "credit" in text and any(word in text for word in ("insufficient", "exhausted", "depleted", "balance", "empty"))


def setup_connection_panel() -> Panel:
    return notice(f"{E['cross']} Connection Setup Failed", SETUP_CONNECTION_ERROR, C["error"])


def parse_start_line(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    group_cart = ""
    address = ""

    # Accept the glaze-style comma input plus pasted two-line, pipe-separated,
    # and plain-space forms. The first URL is always the cart; everything after
    # it belongs to the address and may contain its own commas.
    match = re.match(
        r"^\s*<?(https?://[^\s,|>]+)>?\s*(?:,|\||\r?\n|\s)\s*(.+?)\s*$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if match:
        group_cart, address = match.groups()
    else:
        raise ValueError("Use GROUP LINK, FULL ADDRESS")

    if not group_cart.startswith(("https://", "http://")):
        raise ValueError("The group link must be a full URL")
    if len(address) < 5:
        raise ValueError("Enter the full delivery address")
    return group_cart, normalize_address(address)


def parse_discord_user_id(value: str) -> int:
    raw = value.strip()
    if raw.startswith("<@") and raw.endswith(">"):
        raw = raw[2:-1].lstrip("!")
    if not raw.isdigit() or int(raw) <= 0:
        raise ValueError("Enter a valid Discord user ID")
    return int(raw)


def parse_whitelist_duration(value: str) -> int | None:
    raw = value.strip().lower()
    if not raw or raw in {"permanent", "perm", "forever", "lifetime"}:
        return None
    match = re.fullmatch(
        r"(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)",
        raw,
    )
    if not match:
        raise ValueError("Use a duration such as 30m, 12h, 7d, or 4w")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("Duration must be greater than zero")
    unit = match.group(2)[0]
    multipliers = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = amount * multipliers[unit]
    if seconds > 10 * 365 * 86400:
        raise ValueError("Duration cannot exceed 10 years")
    return seconds


def card_failure_message(value: Any) -> str:
    payload = value.payload if isinstance(value, WoolixApiError) else value
    text = json.dumps(payload, default=str).lower()
    mappings = (
        (("card_declined", "your card was declined"), "Your card was declined."),
        (("insufficient_balance", "insufficient funds"), "Your card has insufficient funds."),
        (("invalid_cvv", "incorrect cvv"), "The card security code is incorrect."),
        (("expired_card", "card expired"), "This card is expired."),
        (("avs_mismatch", "billing zip"), "The billing ZIP does not match the card."),
    )
    for clues, message in mappings:
        if any(clue in text for clue in clues):
            return message
    return ""


def _status_step(job: dict[str, Any]) -> str:
    status = str(job.get("status") or "pending")
    labels = {
        "pending": "Starting…",
        "validating": "Checking your cart…",
        "addressing": "Confirming your address…",
        "cart_syncing": "Loading your items…",
        "previewing": "Finding your best total…",
        "awaiting_config": "Confirming payment details…",
        "draft_ready": "Order ready",
        "placing": "Placing your order…",
        "placed": "Confirming your order…",
    }
    return labels.get(status, "Working on your order…")


def progress_panel(title: str, job: dict[str, Any] | None = None) -> Panel:
    panel = Panel(title=f"{E['loading']} {title}", color=C["warning"], timeout=None)
    if job:
        status = str(job.get("status") or "pending")
        steps = [
            ("validating", "Checking cart"),
            ("addressing", "Confirming delivery"),
            ("cart_syncing", "Loading items"),
            ("previewing", "Finding savings"),
            ("awaiting_config", "Confirming payment"),
            ("draft_ready", "Order ready"),
        ]
        ranks = {name: index for index, (name, _label) in enumerate(steps)}
        current = ranks.get(status, -1)
        for index, (_name, label) in enumerate(steps):
            glyph = E["check"] if index < current or status == "draft_ready" else (E["loading"] if index == current else "▫️")
            panel.line(f"{glyph} {label}")
        panel.rule()
        panel.line(f"› {_status_step(job)}")
    else:
        panel.line(f"› {E['cart']} Starting…")
    panel.footer("Keep this window open — the panel updates automatically.")
    return panel


def api_error_panel(_title: str, error: Exception) -> Panel:
    if is_credit_exhausted(error):
        return setup_connection_panel()
    friendly_title, detail = friendly_error_details(error)
    return notice(f"{E['cross']} {friendly_title}", truncate(detail, 1200), C["error"])


async def render_setup_failure(editor: Any, error: Exception) -> None:
    """Keep the setup transition visible before showing a provider-neutral failure."""
    if is_credit_exhausted(error):
        await asyncio.sleep(SETUP_ERROR_DELAY_SECONDS)
    await editor(view=api_error_panel("Could Not Prepare Order", error))


async def hold_setup_transition(result: Any) -> None:
    if is_credit_exhausted(result):
        await asyncio.sleep(SETUP_ERROR_DELAY_SECONDS)


class StartPanel(Panel):
    def __init__(self, bot: "WoolixBot", owner_id: int):
        super().__init__(
            title=f"{E['rocket']} {bot.settings.bot_name}",
            body="**How should we place this group-cart order?**",
            color=C["primary"],
            timeout=600,
            owner_id=owner_id,
        )
        self.bot = bot
        self.row(
            button("Delivery", self.delivery, style=discord.ButtonStyle.primary, emoji="car"),
            button("Pickup", self.pickup, style=discord.ButtonStyle.secondary, emoji="store"),
        )
        self.footer("A priced draft is created first. Nothing is charged until you confirm the final total.")

    async def delivery(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OrderDetailsModal(self.bot, "delivery"))

    async def pickup(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OrderDetailsModal(self.bot, "pickup"))


class GetATextModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", user_id: int):
        super().__init__(title="Connect GetAText", timeout=300)
        self.bot, self.user_id = bot, user_id
        self.api_key = discord.ui.TextInput(
            label="GetAText API key",
            placeholder="Paste your key from getatext.com",
            required=True,
            min_length=8,
            max_length=128,
        )
        self.add_item(self.api_key)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            self.bot.user_settings_store.set_getatext_key(
                self.user_id,
                self.api_key.value,
            )
            self.bot.queue_event_log(
                "GetAText connection updated",
                level="success",
                user_id=self.user_id,
                details={"configured": True},
            )
            panel = await self.bot.build_dashboard_panel(self.user_id)
        except Exception as exc:
            self.bot.queue_error_log("Save GetAText connection", exc, user_id=self.user_id)
            panel = notice(
                f"{E['cross']} Could Not Save Connection",
                "Your key was not saved. Try again in a moment.",
                C["error"],
            )
        await interaction.edit_original_response(view=panel)


class DashboardPanel(Panel):
    def __init__(
        self,
        bot: "WoolixBot",
        user_id: int,
        *,
        masked_key: str,
        balance: str | None,
        balance_error: str = "",
    ):
        configured = masked_key != "Not connected"
        if not configured:
            title = f"{E['warning']} Setup Required"
            state = "Not connected"
            color = C["warning"]
        elif balance_error:
            title = f"{E['warning']} Connection Needs Attention"
            state = "Needs attention"
            color = C["warning"]
        else:
            title = f"{E['check']} Account Dashboard"
            state = "Connected"
            color = C["success"]
        super().__init__(
            title=title,
            body="Manage your verification connection here.",
            color=color,
            timeout=600,
            owner_id=user_id,
        )
        self.bot, self.user_id = bot, user_id
        self.rule()
        self.line(
            f"{E['phone']} **GetAText · {state}**\n"
            f"**Balance:** {balance or '—'}\n"
            f"**Key:** `{masked_key}`"
        )
        if balance_error:
            self.line(f"{E['warning']} {balance_error}")
        self.rule()
        self.row(
            button(
                "Update GetAText" if configured else "Add GetAText",
                self.set_key,
                style=discord.ButtonStyle.secondary if configured else discord.ButtonStyle.primary,
                emoji="phone",
            ),
            button("Refresh", self.refresh, emoji="refresh"),
            (
                button("Remove", self.remove, style=discord.ButtonStyle.danger, emoji="trash")
                if configured
                else None
            ),
        )
        self.footer("Your key is encrypted when saved and is always masked here.")

    async def set_key(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(GetATextModal(self.bot, self.user_id))

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await interaction.edit_original_response(
            view=await self.bot.build_dashboard_panel(self.user_id)
        )

    async def remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            removed = self.bot.user_settings_store.clear_getatext_key(self.user_id)
            self.bot.queue_event_log(
                "GetAText connection removed",
                level="admin",
                user_id=self.user_id,
                details={"previously_configured": removed},
            )
            panel = await self.bot.build_dashboard_panel(self.user_id)
        except Exception as exc:
            self.bot.queue_error_log("Remove GetAText connection", exc, user_id=self.user_id)
            panel = notice(
                f"{E['cross']} Could Not Remove Connection",
                "Try again in a moment.",
                C["error"],
            )
        await interaction.edit_original_response(view=panel)

class QuickOrderModal(discord.ui.Modal):
    """The complete one-shot create-job input required by the API."""

    def __init__(self, bot: "WoolixBot"):
        super().__init__(title="OVIO DD Order", timeout=600)
        self.bot = bot
        self.group_cart = discord.ui.TextInput(
            label="Group link",
            placeholder="Paste the group-cart share link",
            required=True,
            min_length=4,
            max_length=500,
        )
        self.address = discord.ui.TextInput(
            label="Delivery address",
            placeholder="123 Main St, City, ST 12345",
            required=True,
            min_length=5,
            max_length=300,
        )
        self.card_profile = discord.ui.TextInput(
            label="Card (NUMBER | MM/YY | CVV | ZIP)",
            placeholder="4111111111111111 | 06/31 | 123 | 90250",
            required=True,
            min_length=25,
            max_length=200,
        )
        for field in (self.group_cart, self.address, self.card_profile):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            card = parse_card_profile(self.card_profile.value)
        except ValueError as exc:
            await interaction.response.send_message(f"{E['cross']} {exc}", ephemeral=True)
            return

        session = OrderSession(
            user_id=interaction.user.id,
            group_cart=self.group_cart.value.strip(),
            address=self.address.value.strip(),
            card_last4=card["number"][-4:],
            card=card,
            state=FlowState.CONFIRMING_DRAFT,
        )
        self.bot.set_session(session)
        await self.bot.build_draft(interaction, session)


class OrderDetailsModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", fulfillment: str, session: OrderSession | None = None):
        title = "Delivery Details" if fulfillment == "delivery" else "Pickup Details"
        super().__init__(title=title, timeout=600)
        self.bot = bot
        self.fulfillment = fulfillment
        self.session = session
        self.group_cart = discord.ui.TextInput(
            label="Group-cart link",
            placeholder="Paste the red app group-cart share link",
            required=True,
            min_length=4,
            max_length=500,
            default=session.group_cart if session else None,
        )
        self.name = discord.ui.TextInput(
            label="Name on the order",
            placeholder="Jane Doe",
            required=True,
            min_length=2,
            max_length=100,
            default=session.name if session else None,
        )
        self.address = discord.ui.TextInput(
            label="Delivery address" if fulfillment == "delivery" else "Address (optional for pickup)",
            placeholder="123 Main St, Hawthorne, CA 90250",
            required=fulfillment == "delivery",
            max_length=300,
            default=session.address if session else None,
        )
        self.unit = discord.ui.TextInput(
            label="Apt / suite / unit (optional)",
            placeholder="Apt 4",
            required=False,
            max_length=100,
            default=session.unit if session else None,
        )
        self.promo = discord.ui.TextInput(
            label="Custom promo (optional)",
            placeholder="Tried before the default promo chain",
            required=False,
            max_length=100,
            default=session.promo if session else None,
        )
        for field in (self.group_cart, self.name, self.address, self.unit, self.promo):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        session = self.session or OrderSession(user_id=interaction.user.id)
        session.fulfillment = self.fulfillment
        session.group_cart = self.group_cart.value.strip()
        session.name = self.name.value.strip()
        session.address = self.address.value.strip()
        session.unit = self.unit.value.strip()
        session.promo = self.promo.value.strip()
        session.state = FlowState.DETAILS
        self.bot.set_session(session)
        await interaction.response.edit_message(view=DetailsPanel(self.bot, session))


class DetailsPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession):
        title = f"{E['car']} Delivery Order" if session.fulfillment == "delivery" else f"{E['store']} Pickup Order"
        super().__init__(
            title=title,
            body=customer_block(session),
            color=C["primary"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session = bot, session
        self.rule()
        self.line(f"{E['link']} **Group cart:** `{truncate(session.group_cart, 110)}`")
        if session.promo:
            self.line(f"{E['star']} **Custom promo:** `{truncate(session.promo, 80)}`")
        self.rule()
        self.row(
            button("Continue to Payment", self.payment, style=discord.ButtonStyle.primary, emoji="card"),
            button("Delivery Notes", self.notes, emoji="note"),
            button("Edit Details", self.edit, emoji="back"),
        )
        self.row(button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"))
        self.footer("Payment details are used only for this checkout and are never saved.")

    async def payment(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CardModal(self.bot, self.session))

    async def notes(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NotesModal(self.bot, self.session, configure_live=False))

    async def edit(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(OrderDetailsModal(self.bot, self.session.fulfillment, self.session))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class NotesModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", session: OrderSession, *, configure_live: bool):
        super().__init__(title="Order Notes", timeout=300)
        self.bot, self.session, self.configure_live = bot, session, configure_live
        self.notes = discord.ui.TextInput(
            label="Delivery instructions",
            placeholder="Leave at door, ring the bell, etc.",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            default=session.delivery_note or None,
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.session.delivery_note = self.notes.value.strip()
        if not self.configure_live or not self.session.job_id:
            self.bot.set_session(self.session)
            await interaction.response.edit_message(view=DetailsPanel(self.bot, self.session))
            return
        await self.bot.update_configuration(
            interaction,
            self.session,
            {"note": self.session.delivery_note},
            "Updating Delivery Notes",
        )


class TipModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", session: OrderSession):
        super().__init__(title="Dasher Tip", timeout=300)
        self.bot, self.session = bot, session
        self.tip = discord.ui.TextInput(
            label="Tip in dollars",
            placeholder="3.00 (use 0 to remove)",
            required=True,
            max_length=10,
            default=f"{session.tip_cents / 100:.2f}",
        )
        self.add_item(self.tip)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = round(float(self.tip.value.strip().replace("$", "")) * 100)
            if not 0 <= amount <= 100_000:
                raise ValueError
        except ValueError:
            await interaction.response.send_message("Enter a valid tip between $0 and $1,000.", ephemeral=True)
            return
        self.session.tip_cents = amount
        await self.bot.update_configuration(
            interaction,
            self.session,
            {"tip_cents": amount},
            "Updating Tip",
        )


class CardModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", session: OrderSession):
        super().__init__(title="Card Details", timeout=300)
        self.bot, self.session = bot, session
        self.number = discord.ui.TextInput(
            label="Card Number",
            placeholder="4111111111111111",
            required=True,
            min_length=13,
            max_length=24,
        )
        self.expiry = discord.ui.TextInput(
            label="Expiry (MM/YY)",
            placeholder="06/31",
            required=True,
            min_length=4,
            max_length=7,
        )
        self.cvv = discord.ui.TextInput(
            label="CVV",
            placeholder="123",
            required=True,
            min_length=3,
            max_length=4,
        )
        self.zip_code = discord.ui.TextInput(
            label="Billing ZIP",
            placeholder="90250",
            required=True,
            min_length=3,
            max_length=12,
        )
        for field in (self.number, self.expiry, self.cvv, self.zip_code):
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            card = parse_card(self.number.value, self.expiry.value, self.cvv.value, self.zip_code.value)
        except ValueError as exc:
            await interaction.response.send_message(f"{E['cross']} {exc}", ephemeral=True)
            return
        self.session.card = card
        self.session.card_last4 = card["number"][-4:]
        self.session.state = FlowState.CONFIRMING_DRAFT
        self.bot.set_session(self.session)
        await interaction.response.edit_message(view=ConfirmDraftPanel(self.bot, self.session))


class ConfirmDraftPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession):
        super().__init__(
            title=f"{E['sparkles']} Confirm Order",
            body=customer_block(session),
            color=C["warning"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session = bot, session
        self.rule()
        self.line(
            f"{E['link']} **Group cart:** `{truncate(session.group_cart, 110)}`\n"
            f"{payment_line(session)}"
        )
        self.rule()
        self.row(
            button("Review Order", self.confirm, style=discord.ButtonStyle.success, emoji="rocket"),
            button("Back", self.back, emoji="back"),
            button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
        )
        self.footer("This does not place or charge the order. You review the final total next.")

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.bot.build_draft(interaction, self.session)

    async def back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=DetailsPanel(self.bot, self.session))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class DraftPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        cart = job.get("cart") or {}
        items = cart.get("items") or []
        super().__init__(
            title=f"{E['cart']} Your Cart ({len(items)})",
            body=(
                f"{E['check']} **Order priced successfully**\n"
                f"{E['store']} **{truncate(cart.get('store_name') or 'Store', 100)}**"
            ),
            color=C["success"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        self.rule()
        self.line(customer_block(session))
        self.line(payment_line(session))
        self.rule()
        self.line(item_lines(job))
        self.rule()
        self.line(price_lines(job))
        self.line(f"{E['money']} **Checkout fee:** {money(CHECKOUT_FEE_CENTS)} · billed by DM tonight")
        self.rule()
        self.row(
            (
                button("Place Order", self.confirm, style=discord.ButtonStyle.success, emoji="rocket")
                if session.card_last4
                else button("Add Card", self.add_card, style=discord.ButtonStyle.primary, emoji="card")
            ),
            button("Delivery Notes", self.notes, emoji="note"),
            button("Tip", self.tip, emoji="tip"),
        )
        self.row(
            button("Leave at Door", self.leave, emoji="deliver"),
            button("Hand it to Me", self.meet, emoji="person"),
            button(
                "Switch to Pickup" if session.fulfillment == "delivery" else "Switch to Delivery",
                self.switch_fulfillment,
                emoji="store" if session.fulfillment == "delivery" else "car",
            ),
            button("New Cart", self.rebuild, emoji="refresh"),
            button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
        )
        expires = job.get("expires_in_seconds")
        expiry_note = f" · draft expires in ~{max(0, int(expires or 0)) // 60} min" if expires is not None else ""
        footer = "Review your order carefully" if session.card_last4 else "Add your card here when you are ready to check out"
        self.footer(f"{footer}{expiry_note}")

    async def confirm(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=PlaceOrderConfirmPanel(self.bot, self.session, self.job))

    async def add_card(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReplacementCardModal(self.bot, self.session))

    async def notes(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(NotesModal(self.bot, self.session, configure_live=True))

    async def tip(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(TipModal(self.bot, self.session))

    async def leave(self, interaction: discord.Interaction) -> None:
        await self.bot.update_job_action(interaction, self.session, "Setting Dropoff", self.bot.api.set_dropoff, "leave")

    async def meet(self, interaction: discord.Interaction) -> None:
        await self.bot.update_job_action(interaction, self.session, "Setting Dropoff", self.bot.api.set_dropoff, "meet")

    async def switch_fulfillment(self, interaction: discord.Interaction) -> None:
        target = "pickup" if self.session.fulfillment == "delivery" else "delivery"
        await self.bot.update_job_action(
            interaction,
            self.session,
            f"Switching to {target.title()}",
            self.bot.api.set_fulfillment,
            target,
            fulfillment=target,
        )

    async def rebuild(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RebuildModal(self.bot, self.session))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class CartMismatchPanel(Panel):
    """Fail closed when the priced subtotal contains a hidden duplicate item."""

    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        extra_cents = suspected_duplicate_item_cents(job) or 0
        super().__init__(
            title=f"{E['warning']} Cart Total Changed",
            body=(
                "Checkout was stopped because the subtotal includes "
                f"an extra **{money(extra_cents)}** that is not shown in the item list."
            ),
            color=C["error"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        self.rule()
        self.line(item_lines(job))
        self.rule()
        self.line(price_lines(job))
        self.rule()
        self.row(
            button("Use Fresh Cart", self.new_cart, style=discord.ButtonStyle.primary, emoji="refresh"),
            button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
        )
        self.footer("Nothing was charged. Use a fresh group cart so no item can be counted twice.")

    async def new_cart(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RebuildModal(self.bot, self.session))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class RebuildModal(discord.ui.Modal):
    def __init__(self, bot: "WoolixBot", session: OrderSession):
        super().__init__(title="Replace Group Cart", timeout=300)
        self.bot, self.session = bot, session
        self.link = discord.ui.TextInput(
            label="New group-cart link",
            placeholder="Paste a fresh group-cart link",
            required=True,
            min_length=4,
            max_length=500,
        )
        self.add_item(self.link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=progress_panel("Rebuilding Cart"))
        try:
            rebuilt = await self.bot.api.rebuild_job(self.session.job_id, self.link.value.strip())
            self.session.group_cart = self.link.value.strip()
            self.session.job_id = str(rebuilt.get("new_job_id") or self.session.job_id)
            self.session.integrity_rebuild_attempted = False
            self.session.state = FlowState.BUILDING_DRAFT
            self.bot.set_session(self.session)
            job = await self.bot.wait_for_draft(interaction, self.session)
            await interaction.edit_original_response(view=self.bot.panel_for_job(self.session, job))
        except Exception as exc:
            self.bot.queue_error_log(
                "Rebuild cart",
                exc,
                user_id=self.session.user_id,
                job_id=self.session.job_id,
            )
            await interaction.edit_original_response(view=api_error_panel("Could Not Rebuild Cart", exc))


class PlaceOrderConfirmPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        extra_cents = suspected_duplicate_item_cents(job)
        super().__init__(
            title=f"{E['warning'] if extra_cents else E['sparkles']} {'Cart Total Changed' if extra_cents else 'Confirm Order'}",
            body=customer_block(session),
            color=C["error"] if extra_cents else C["warning"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        self.rule()
        self.line(f"{item_lines(job)}\n\n{price_lines(job)}\n{payment_line(session)}")
        if extra_cents:
            self.line(
                f"{E['warning']} Checkout blocked: the subtotal contains an extra "
                f"**{money(extra_cents)}** not shown above."
            )
        self.rule()
        self.row(
            None if extra_cents else button("Place Order", self.place, style=discord.ButtonStyle.success, emoji="rocket"),
            button("Back", self.back, emoji="back"),
            button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
        )
        self.footer(
            "Nothing was charged. Return to the cart and use a fresh group link."
            if extra_cents
            else "This places a REAL order and charges the card. Check the final total above."
        )

    async def place(self, interaction: discord.Interaction) -> None:
        await self.bot.place_order(interaction, self.session)

    async def back(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=DraftPanel(self.bot, self.session, self.job))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class ReplacementCardModal(CardModal):
    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            card = parse_card(self.number.value, self.expiry.value, self.cvv.value, self.zip_code.value)
        except ValueError as exc:
            await interaction.response.send_message(f"{E['cross']} {exc}", ephemeral=True)
            return
        await interaction.response.edit_message(view=progress_panel("Updating Card"))
        self.bot.remember_main_interaction(interaction)
        try:
            job = await self.bot.api.configure_job(self.session.job_id, {"card": card})
            self.session.card = None
            self.session.job = job
            if job.get("card_error"):
                self.session.card_last4 = ""
            else:
                self.session.card_last4 = card["number"][-4:]
                if job.get("status") != "draft_ready":
                    job = await self.bot.wait_for_draft(interaction, self.session)
                    self.session.job = job
            panel = self.bot.panel_for_job(self.session, job)
            self.bot.set_session(self.session)
            await interaction.edit_original_response(view=panel)
        except Exception as exc:
            self.bot.queue_error_log(
                "Add card to draft",
                exc,
                user_id=self.session.user_id,
                job_id=self.session.job_id,
            )
            friendly = card_failure_message(exc)
            if friendly:
                job = dict(self.session.job)
                job["card_error"] = {"message": friendly}
                self.session.card_last4 = ""
                self.session.job = job
                self.session.state = FlowState.DRAFT_READY
                self.bot.set_session(self.session)
                await interaction.edit_original_response(view=CardRetryPanel(self.bot, self.session, job))
            else:
                await interaction.edit_original_response(view=api_error_panel("Could Not Update Card", exc))


class CardRetryPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        error = job.get("card_error") or {}
        terminal = bool(error.get("terminal")) if isinstance(error, dict) else False
        super().__init__(
            title=f"{E['cross']} {friendly_error_details(job)[0]}",
            body=error_message(job),
            color=C["error"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        self.row(
            button("Try New Card", self.new_card, style=discord.ButtonStyle.primary, emoji="card"),
            None if terminal else button("Retry Same Card", self.retry, emoji="refresh"),
            button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
        )
        self.footer("Nothing was charged and no order was placed.")

    async def new_card(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ReplacementCardModal(self.bot, self.session))

    async def retry(self, interaction: discord.Interaction) -> None:
        await self.bot.place_order(interaction, self.session)

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class RetryOrderPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        friendly_title, friendly_message = friendly_error_details(job)
        super().__init__(
            title=f"{E['warning']} {friendly_title}",
            body=friendly_message,
            color=C["warning"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        if job.get("status") == "draft_ready":
            self.row(
                button("Try Again", self.retry, style=discord.ButtonStyle.primary, emoji="refresh"),
                button("New Cart", self.rebuild, emoji="cart"),
                button("Cancel", self.cancel, style=discord.ButtonStyle.danger, emoji="close"),
            )
            self.footer("Nothing was charged and no order was placed.")
        else:
            self.footer("Send !start for a new order.")

    async def retry(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=PlaceOrderConfirmPanel(self.bot, self.session, self.job))

    async def rebuild(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RebuildModal(self.bot, self.session))

    async def cancel(self, interaction: discord.Interaction) -> None:
        await self.bot.cancel_session(interaction, self.session)


class UnverifiedPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        super().__init__(
            title=f"{E['clock']} Finishing Your Order",
            body=(
                f"{E['check']} **Checkout submitted**\n"
                f"{E['refresh']} **Waiting for store confirmation**\n\n"
                "Keep this panel open—it updates automatically."
            ),
            color=C["primary"],
            timeout=600,
            owner_id=session.user_id,
        )
        self.bot, self.session, self.job = bot, session, job
        self.footer("Please do not submit the order again while confirmation is pending.")

    async def refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=progress_panel("Refreshing Status", self.job))
        try:
            job = await self.bot.api.get_job(self.session.job_id)
            self.session.job = job
            self.bot.set_session(self.session)
            await interaction.edit_original_response(view=self.bot.panel_for_job(self.session, job))
        except Exception as exc:
            self.bot.queue_error_log(
                "Refresh order status",
                exc,
                user_id=self.session.user_id,
                job_id=self.session.job_id,
            )
            await interaction.edit_original_response(view=api_error_panel("Could Not Refresh Status", exc))


def _account_line(job: dict[str, Any]) -> str:
    account = job.get("account_email")
    return f"{E['mail']} **Account:** `{truncate(account, 180)}`" if account else "Account details are still settling."


class OrderPlacedPanel(Panel):
    def __init__(self, bot: "WoolixBot", session: OrderSession, job: dict[str, Any]):
        cart = job.get("cart") or {}
        super().__init__(
            title=f"{E['check']} Order Placed",
            body=f"{E['store']} **{truncate(cart.get('store_name') or 'Your store', 100)}** is preparing the order.",
            color=C["success"],
            timeout=None,
            owner_id=session.user_id,
        )
        self.rule()
        self.line(
            f"{_account_line(job)}\n"
            f"{E['money']} **Total:** {money(cart.get('client_total_cents'))}"
        )
        tracking = order_tracker_url(job)
        if tracking.startswith("http"):
            self.row(link_button("Track Order", tracking, emoji="link"))
        self.footer("Enjoy your order!")


class AdminJobPanel(Panel):
    def __init__(self, job: dict[str, Any], *, title: str = "Admin Job View"):
        cart = job.get("cart") or {}
        super().__init__(
            title=f"{E['info']} {title}",
            body=(
                f"**Job:** `{truncate(job.get('job_id'), 120)}`\n"
                f"**Status:** `{truncate(job.get('status'), 60)}`\n"
                f"**Payment:** `{truncate(job.get('payment_status') or 'not settled', 60)}`"
            ),
            color=C["info"],
            timeout=None,
        )
        if cart:
            self.rule()
            self.line(
                f"{E['store']} **{truncate(cart.get('store_name') or 'Store', 100)}**\n"
                f"{item_lines(job)}\n\n{price_lines(job)}"
            )
        if job.get("error") or job.get("card_error") or job.get("place_order_error"):
            self.rule()
            self.line(f"{E['warning']} {error_message(job)}")
        tracking = order_tracker_url(job)
        if tracking.startswith("http"):
            self.row(link_button("Track Order", tracking, emoji="link"))
        self.footer("Owner-only order job inspection")


class AdminGroup(app_commands.Group):
    def __init__(self, bot: "WoolixBot"):
        super().__init__(name="admin", description="Owner-only OVIO DD management")
        self.bot = bot

    async def guard(self, interaction: discord.Interaction) -> bool:
        if self.bot.is_owner(interaction.user.id):
            return True
        await interaction.response.send_message("Only a configured bot owner can use `/admin`.", ephemeral=True)
        return False

    @app_commands.command(name="overview", description="Show bot, session, access, and emoji status")
    async def overview(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        try:
            emoji_count = len(await self.bot.fetch_application_emojis())
        except discord.HTTPException:
            emoji_count = 0
        active = [
            session for session in self.bot.sessions.values()
            if session.state not in {FlowState.PLACED, FlowState.CANCELLED, FlowState.FAILED}
        ]
        access = (
            f"{len(self.bot.settings.allowed_user_ids)} allowed user(s)"
            if self.bot.settings.allowed_user_ids else "Owners only"
        )
        panel = Panel(
            title=f"{E['menu']} Admin Overview",
            body=(
                f"{E['check']} **Bot:** `{self.bot.user or 'connecting'}`\n"
                f"{E['cart']} **Active orders:** `{len(active)}`\n"
                f"{E['receipt']} **Saved sessions:** `{len(self.bot.sessions)}`\n"
                f"{E['person']} **Access:** {access}\n"
                f"{E['sparkles']} **Application emojis:** `{emoji_count}`\n"
                f"{E['warning']} **Maintenance:** `{'ON' if self.bot.settings.maintenance else 'OFF'}`"
            ),
            color=C["primary"],
            timeout=None,
        )
        panel.footer("Use the other /admin subcommands to manage each area.")
        await interaction.response.send_message(view=panel, ephemeral=True)

    @app_commands.command(name="config", description="Show sanitized runtime configuration")
    async def config(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        settings = self.bot.settings
        panel = Panel(
            title=f"{E['lock']} Runtime Configuration",
            body=(
                f"**Bot name:** `{truncate(settings.bot_name, 100)}`\n"
                f"**Command scope:** `{'guild ' + str(settings.discord_guild_id) if settings.discord_guild_id else 'global'}`\n"
                f"**Order API:** `configured`\n"
                f"**API timeout:** `{settings.api_timeout_seconds:g}s`\n"
                f"**Draft timeout:** `{settings.draft_timeout_seconds:g}s`\n"
                f"**Settlement timeout:** `{settings.settlement_timeout_seconds:g}s`\n"
                f"**Poll interval:** `{settings.poll_interval_seconds:g}s`\n"
                f"**Configured owners:** `{len(settings.owner_user_ids)}`\n"
                f"**Configured users:** `{len(settings.allowed_user_ids)}`\n"
                f"**Success webhook:** `{'configured' if settings.success_webhook_url else 'not configured'}`\n"
                f"**Discord token:** `configured`\n"
                f"**Order API key:** `configured`"
            ),
            color=C["neutral"],
            timeout=None,
        )
        panel.footer("Secrets are intentionally never rendered by admin commands.")
        await interaction.response.send_message(view=panel, ephemeral=True)

    @app_commands.command(name="users", description="List configured owners and allowed users")
    async def users(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return

        def mentions(values: set[int]) -> str:
            return "\n".join(f"• <@{user_id}> (`{user_id}`)" for user_id in sorted(values)) or "_None configured_"

        panel = Panel(title=f"{E['person']} Access Control", color=C["info"], timeout=None)
        panel.line(f"**Configured owners**\n{mentions(self.bot.settings.owner_user_ids)}")
        panel.rule()
        panel.line(f"**Allowed users**\n{mentions(self.bot.settings.allowed_user_ids)}")
        panel.footer("The bot owner can always use !start. An empty user list means owners only.")
        await interaction.response.send_message(view=panel, ephemeral=True)

    @app_commands.command(name="allow-user", description="Add a user to the bot allowlist")
    @app_commands.describe(user="User who should be allowed to place orders")
    async def allow_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self.guard(interaction):
            return
        already = user.id in self.bot.settings.allowed_user_ids
        self.bot.settings.allowed_user_ids.add(user.id)
        self.bot.settings.allowed_user_expirations.pop(user.id, None)
        self.bot.settings.save_runtime()
        message = f"{user.mention} was already allowed." if already else f"Allowed {user.mention}."
        await interaction.response.send_message(view=notice(f"{E['check']} User Allowed", message, C["success"]), ephemeral=True)

    @app_commands.command(name="revoke-user", description="Remove a user from the bot allowlist")
    @app_commands.describe(user="User to remove from the allowlist")
    async def revoke_user(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self.guard(interaction):
            return
        existed = user.id in self.bot.settings.allowed_user_ids
        self.bot.settings.allowed_user_ids.discard(user.id)
        self.bot.settings.allowed_user_expirations.pop(user.id, None)
        self.bot.settings.save_runtime()
        message = f"Removed {user.mention}." if existed else f"{user.mention} was not in the allowlist."
        if not self.bot.settings.allowed_user_ids:
            message += " The allowlist is empty, so only owners can start orders."
        await interaction.response.send_message(view=notice(f"{E['check']} Access Updated", message, C["success"]), ephemeral=True)

    @app_commands.command(name="add-owner", description="Add a persistent bot owner")
    @app_commands.describe(user="User who should receive owner privileges")
    async def add_owner(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self.guard(interaction):
            return
        self.bot.settings.owner_user_ids.add(user.id)
        self.bot.settings.save_runtime()
        await interaction.response.send_message(
            view=notice(f"{E['check']} Owner Added", f"{user.mention} can now use every `/admin` command.", C["success"]),
            ephemeral=True,
        )

    @app_commands.command(name="remove-owner", description="Remove a persistent bot owner")
    @app_commands.describe(user="Configured owner to remove")
    async def remove_owner(self, interaction: discord.Interaction, user: discord.User) -> None:
        if not await self.guard(interaction):
            return
        if user.id == interaction.user.id and not self.bot.is_application_owner(interaction.user.id):
            await interaction.response.send_message("You cannot remove your own configured owner access.", ephemeral=True)
            return
        self.bot.settings.owner_user_ids.discard(user.id)
        self.bot.settings.save_runtime()
        await interaction.response.send_message(
            view=notice(f"{E['check']} Owner Removed", f"Removed configured owner access from {user.mention}.", C["success"]),
            ephemeral=True,
        )

    @app_commands.command(name="maintenance", description="Enable or disable maintenance mode")
    @app_commands.describe(enabled="Whether customer commands should be paused", message="Optional maintenance notice")
    async def maintenance(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        message: str | None = None,
    ) -> None:
        if not await self.guard(interaction):
            return
        self.bot.settings.maintenance = enabled
        if message and message.strip():
            self.bot.settings.maintenance_message = message.strip()[:1000]
        self.bot.settings.save_runtime()
        color = C["warning"] if enabled else C["success"]
        await interaction.response.send_message(
            view=notice(
                f"{E['warning'] if enabled else E['check']} Maintenance {'Enabled' if enabled else 'Disabled'}",
                self.bot.settings.maintenance_message if enabled else "Customer commands and panels are available again.",
                color,
            ),
            ephemeral=True,
        )

    @app_commands.command(name="jobs", description="List locally tracked order jobs")
    async def jobs(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        sessions = sorted(self.bot.sessions.values(), key=lambda session: session.user_id)
        panel = Panel(title=f"{E['receipt']} Tracked Jobs", color=C["info"], timeout=None)
        if not sessions:
            panel.line("_No saved sessions._")
        for session in sessions[:20]:
            panel.line(
                f"<@{session.user_id}> · `{session.job_id or 'no job'}` · "
                f"**{session.state.value.replace('_', ' ').title()}**"
            )
        if len(sessions) > 20:
            panel.line(f"-# …and {len(sessions) - 20} more")
        panel.footer("Use /admin job with a job ID for the live order state.")
        await interaction.response.send_message(view=panel, ephemeral=True)

    @app_commands.command(name="job", description="Fetch an order job by ID")
    @app_commands.describe(job_id="Order job ID")
    async def job(self, interaction: discord.Interaction, job_id: str) -> None:
        if not await self.guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            job = await self.bot.api.get_job(job_id.strip())
            await interaction.edit_original_response(view=AdminJobPanel(job))
        except Exception as exc:
            self.bot.queue_error_log("Admin job lookup", exc, user_id=interaction.user.id, job_id=job_id.strip())
            await interaction.edit_original_response(view=api_error_panel("Could Not Load Job", exc))

    @app_commands.command(name="cancel-job", description="Cancel an order job by ID")
    @app_commands.describe(job_id="Order job ID", confirm="Must be true to send the cancellation")
    async def cancel_job(self, interaction: discord.Interaction, job_id: str, confirm: bool) -> None:
        if not await self.guard(interaction):
            return
        if not confirm:
            await interaction.response.send_message("No cancellation sent. Set `confirm` to `True`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            result = await self.bot.api.cancel_job(job_id.strip())
            for session in self.bot.sessions.values():
                if session.job_id == job_id.strip():
                    session.state = FlowState.CANCELLED
                    self.bot.set_session(session)
                    break
            await interaction.edit_original_response(
                view=notice(
                    f"{E['check']} Cancellation Sent",
                    f"Job `{truncate(job_id, 120)}`: `{truncate(result.get('status') or 'signal accepted', 100)}`",
                    C["success"],
                )
            )
        except Exception as exc:
            self.bot.queue_error_log("Admin job cancellation", exc, user_id=interaction.user.id, job_id=job_id.strip())
            await interaction.edit_original_response(view=api_error_panel("Could Not Cancel Job", exc))

    @app_commands.command(name="proceed-job", description="Place and charge a draft-ready order job")
    @app_commands.describe(job_id="Order job ID", confirm="Must be true because this places a real order")
    async def proceed_job(self, interaction: discord.Interaction, job_id: str, confirm: bool) -> None:
        if not await self.guard(interaction):
            return
        if not confirm:
            await interaction.response.send_message(
                "No order placed. Set `confirm` to `True` to charge the card and submit the real order.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            job = await self.bot.api.proceed_job(job_id.strip())
            await interaction.edit_original_response(view=AdminJobPanel(job, title="Proceed Result"))
        except Exception as exc:
            self.bot.queue_error_log("Admin job placement", exc, user_id=interaction.user.id, job_id=job_id.strip())
            await interaction.edit_original_response(view=api_error_panel("Could Not Proceed Job", exc))

    @app_commands.command(name="clear-session", description="Forget a local user session without cancelling the order job")
    @app_commands.describe(user="User whose local session should be removed", confirm="Must be true")
    async def clear_session(self, interaction: discord.Interaction, user: discord.User, confirm: bool) -> None:
        if not await self.guard(interaction):
            return
        if not confirm:
            await interaction.response.send_message("No session removed. Set `confirm` to `True`.", ephemeral=True)
            return
        removed = self.bot.sessions.pop(user.id, None)
        self.bot.session_store.save(self.bot.sessions.values())
        message = f"Forgot the local session for {user.mention}." if removed else f"No local session existed for {user.mention}."
        await interaction.response.send_message(
            view=notice(f"{E['trash']} Session Cleared", message, C["neutral"]),
            ephemeral=True,
        )

    @app_commands.command(name="sync-emojis", description="Upload/reuse every bundled application emoji")
    async def sync_emojis(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            applied = await sync_app_emojis(self.bot)
            await interaction.edit_original_response(
                view=notice(
                    f"{E['sparkles']} Emojis Synchronized",
                    f"Using `{len(applied)}` custom application emoji(s). Missing assets were uploaded and existing ones were reused.",
                    C["success"],
                )
            )
        except Exception as exc:
            self.bot.queue_error_log("Admin emoji sync", exc, user_id=interaction.user.id)
            await interaction.edit_original_response(view=api_error_panel("Emoji Sync Failed", exc))


class WoolixBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        super().__init__(command_prefix="!", intents=intents, description=settings.bot_name)
        self.settings = settings
        configure_tracking(settings.tracker_base_url, settings.tracker_slug_secret)
        self.api = WoolixApiClient(
            settings.woolix_api_base_url,
            settings.woolix_api_key,
            settings.api_timeout_seconds,
        )
        self.session_store = SessionStore(DATA_DIR / "sessions.json")
        self.sessions = self.session_store.load()
        self.logged_session_states: dict[int, tuple[str, str]] = {
            user_id: (session.state.value, session.job_id)
            for user_id, session in self.sessions.items()
        }
        self.billing_store = BillingStore(
            DATA_DIR / "checkout_fees.json",
            settings.database_url,
        )
        self.user_settings_store = UserSettingsStore(
            settings.database_url,
            settings.tracker_slug_secret,
        )
        self.user_locks: dict[int, asyncio.Lock] = {}
        self.announcement_locks: dict[int, asyncio.Lock] = {}
        self.main_messages: dict[int, discord.Message] = {}
        self.collecting_users: set[int] = set()
        self.reported_error_fingerprints: set[str] = set()
        self.success_monitor_task: asyncio.Task[None] | None = None
        self.nightly_billing_task: asyncio.Task[None] | None = None
        self.notifier = SuccessNotifier(settings.success_webhook_url, settings.bot_name)

    def lock_for(self, user_id: int) -> asyncio.Lock:
        return self.user_locks.setdefault(user_id, asyncio.Lock())

    def announcement_lock_for(self, user_id: int) -> asyncio.Lock:
        return self.announcement_locks.setdefault(user_id, asyncio.Lock())

    def remember_main_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.message is not None:
            self.main_messages[interaction.user.id] = interaction.message

    async def edit_main_panel(self, user_id: int, panel: Panel) -> bool:
        message = self.main_messages.get(user_id)
        if message is None:
            return False
        try:
            await message.edit(view=panel)
            return True
        except (discord.NotFound, discord.HTTPException):
            self.main_messages.pop(user_id, None)
            return False

    def set_session(self, session: OrderSession) -> None:
        self.sessions[session.user_id] = session
        self.session_store.save(self.sessions.values())
        current = (session.state.value, session.job_id)
        previous = self.logged_session_states.get(session.user_id)
        self.logged_session_states[session.user_id] = current
        if previous != current:
            self.queue_event_log(
                "Order session updated",
                user_id=session.user_id,
                job_id=session.job_id,
                details={
                    "previous_state": previous[0] if previous else "new",
                    "state": session.state.value,
                    "job_assigned": bool(session.job_id),
                },
            )

    def clear_session(self, user_id: int, *, expected_job_id: str = "") -> bool:
        """Remove a finished checkout without allowing an old panel to erase a newer one."""
        active = self.sessions.get(user_id)
        if active is None:
            return True
        if expected_job_id and active.job_id != expected_job_id:
            return False
        self.sessions.pop(user_id, None)
        self.logged_session_states.pop(user_id, None)
        self.session_store.save(self.sessions.values())
        return True

    async def report_event(
        self,
        event: str,
        *,
        level: str = "info",
        user_id: int | None = None,
        job_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Send a sanitized operational event to the private log channel."""
        channel_id = self.settings.log_channel_id
        if not channel_id:
            return
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        content = event_log_message(
            event,
            level=level,
            user_id=user_id,
            job_id=job_id,
            details=details,
            timestamp=timestamp,
        )
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            await channel.send(
                content=content,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as exc:
            logger.warning("Could not send activity log to channel %s: %s", channel_id, exc)

    def queue_event_log(
        self,
        event: str,
        *,
        level: str = "info",
        user_id: int | None = None,
        job_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Queue a private activity log without delaying the checkout flow."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Could not queue activity log outside an active event loop: %s", event)
            return
        task_name = hashlib.sha1(f"{event}|{user_id}|{job_id}".encode("utf-8")).hexdigest()[:8]
        loop.create_task(
            self.report_event(
                event,
                level=level,
                user_id=user_id,
                job_id=job_id,
                details=details,
            ),
            name=f"activity-log-{task_name}",
        )

    async def report_error(
        self,
        context: str,
        error: Any,
        *,
        user_id: int | None = None,
        job_id: str = "",
    ) -> None:
        channel_id = self.settings.log_channel_id
        if not channel_id:
            return
        diagnostic = diagnostic_dump(error)
        fingerprint_source = f"{context}|{user_id}|{job_id}|{diagnostic}"
        fingerprint = hashlib.sha256(fingerprint_source.encode("utf-8")).hexdigest()
        if fingerprint in self.reported_error_fingerprints:
            return
        if len(self.reported_error_fingerprints) >= 2000:
            self.reported_error_fingerprints.clear()
        self.reported_error_fingerprints.add(fingerprint)
        reference = fingerprint[:10].upper()
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            summary = (
                f"🚨 **OVIO DD Error** · `{reference}`\n"
                f"**Context:** {context}\n"
                f"**User ID:** `{user_id or 'unknown'}`\n"
                f"**Job ID:** `{job_id or 'not assigned'}`\n"
                f"**Time:** `{timestamp}`"
            )
            attachment = discord.File(
                io.BytesIO(diagnostic.encode("utf-8")),
                filename=f"ovio-error-{reference}.log",
            )
            await channel.send(
                content=summary,
                file=attachment,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as report_exc:
            self.reported_error_fingerprints.discard(fingerprint)
            logger.error("Could not send error %s to log channel %s: %s", reference, channel_id, report_exc)

    def queue_error_log(
        self,
        context: str,
        error: Any,
        *,
        user_id: int | None = None,
        job_id: str = "",
    ) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.error("Could not queue error log outside an active event loop: %s", context)
            return
        loop.create_task(
            self.report_error(context, error, user_id=user_id, job_id=job_id),
            name=f"error-log-{hashlib.sha1(context.encode('utf-8')).hexdigest()[:8]}",
        )

    def queue_job_error_log(self, session: OrderSession, job: dict[str, Any]) -> None:
        status = str(job.get("status") or "")
        payment = str(job.get("payment_status") or "")
        if not (
            job.get("error")
            or job.get("card_error")
            or job.get("place_order_error")
            or status in {"failed", "expired"}
            or payment in {"declined", "failed", "unverified"}
        ):
            return
        snapshot = {
            "status": status,
            "substatus": job.get("substatus"),
            "payment_status": payment,
            "error": job.get("error"),
            "card_error": job.get("card_error"),
            "place_order_error": job.get("place_order_error"),
        }
        self.queue_error_log(
            "Order job returned an error",
            snapshot,
            user_id=session.user_id,
            job_id=session.job_id,
        )

    def is_application_owner(self, user_id: int) -> bool:
        application = getattr(self, "application", None)
        owner = getattr(application, "owner", None)
        if owner and owner.id == user_id:
            return True
        team = getattr(application, "team", None)
        members = getattr(team, "members", []) if team else []
        return any(member.id == user_id for member in members)

    def is_owner(self, user_id: int) -> bool:
        return user_id in self.settings.owner_user_ids or self.is_application_owner(user_id)

    async def build_dashboard_panel(self, user_id: int) -> Panel:
        """Build a private dashboard snapshot without ever rendering the raw key."""
        try:
            key = self.user_settings_store.get_getatext_key(user_id)
        except Exception as exc:
            self.queue_error_log("Load GetAText connection", exc, user_id=user_id)
            return notice(
                f"{E['cross']} Dashboard Unavailable",
                "Your settings could not be loaded. Try again in a moment.",
                C["error"],
            )

        balance = None
        balance_error = ""
        if key:
            try:
                amount = await getatext_balance(key)
                balance = f"${amount:,.2f}"
            except GetATextError as exc:
                balance_error = str(exc)
                self.queue_event_log(
                    "GetAText balance unavailable",
                    level="warning",
                    user_id=user_id,
                    details={"configured": True},
                )
            except Exception as exc:
                balance_error = "Balance is temporarily unavailable."
                self.queue_error_log("Read GetAText balance", exc, user_id=user_id)

        return DashboardPanel(
            self,
            user_id,
            masked_key=mask_secret(key),
            balance=balance,
            balance_error=balance_error,
        )

    async def maybe_announce_success(self, session: OrderSession, job: dict[str, Any]) -> None:
        if job.get("payment_status") != "succeeded":
            return
        async with self.announcement_lock_for(session.user_id):
            if session.success_announced:
                return
            post_task = asyncio.create_task(
                self.notifier.send(session, job),
                name=f"success-post-{session.job_id}",
            )
            billing_task = asyncio.create_task(
                asyncio.to_thread(self.billing_store.record_checkout, session.user_id, session.job_id),
                name=f"success-billing-{session.job_id}",
            )
            post_result, billing_result = await asyncio.gather(
                post_task,
                billing_task,
                return_exceptions=True,
            )
            if isinstance(post_result, Exception):
                exc = post_result
                logger.error("Checkout success webhook failed for job %s: %s", session.job_id, exc)
                self.queue_error_log(
                    "Public checkout success post",
                    exc,
                    user_id=session.user_id,
                    job_id=session.job_id,
                )
                return
            fee_recorded = False
            if isinstance(billing_result, Exception):
                logger.error("Checkout fee recording failed for job %s: %s", session.job_id, billing_result)
                self.queue_error_log(
                    "Record checkout fee",
                    billing_result,
                    user_id=session.user_id,
                    job_id=session.job_id,
                )
            else:
                fee_recorded = bool(billing_result)
            session.success_announced = True
            session.state = FlowState.PLACED
            session.job = job
            self.set_session(session)
            cart = job.get("cart") or {}
            self.queue_event_log(
                "Checkout completed",
                level="success",
                user_id=session.user_id,
                job_id=session.job_id,
                details={
                    "store": str(cart.get("store_name") or "Group order")[:100],
                    "order_total": money(cart.get("client_total_cents")),
                    "checkout_fee_recorded": fee_recorded,
                    "public_success_posted": True,
                    "tracking_link_ready": bool(order_tracker_url(job)),
                },
            )

    async def monitor_checkout_successes(self) -> None:
        """Keep the original panel live through draft creation and final settlement."""
        await self.wait_until_ready()
        while not self.is_closed():
            candidates = [
                session for session in self.sessions.values()
                if session.job_id
                and not session.success_announced
                and session.state in {FlowState.BUILDING_DRAFT, FlowState.PLACING}
            ]
            for session in candidates:
                if self.lock_for(session.user_id).locked():
                    continue
                try:
                    job = await self.api.get_job(session.job_id)
                    session.job = job
                    if job.get("payment_status") == "succeeded":
                        await self.maybe_announce_success(session, job)
                    panel = self.panel_for_job(session, job)
                    self.set_session(session)
                    await self.edit_main_panel(session.user_id, panel)
                except Exception as exc:
                    logger.warning("Background checkout check failed for job %s: %s", session.job_id, exc)
                    self.queue_error_log(
                        "Background order monitor",
                        exc,
                        user_id=session.user_id,
                        job_id=session.job_id,
                    )
            await asyncio.sleep(
                max(1.0, min(SUCCESS_MONITOR_MAX_INTERVAL_SECONDS, self.settings.poll_interval_seconds))
            )

    async def send_nightly_billing_reminders(self) -> None:
        """Demand unpaid checkout fees after each 11 PM PT cutoff, with catch-up."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                summaries = self.billing_store.due_summaries()
            except Exception as exc:
                logger.warning("Nightly billing lookup failed: %s", exc)
                self.queue_error_log("Nightly billing lookup", exc)
                await asyncio.sleep(60)
                continue
            for summary in summaries:
                try:
                    user = await self.fetch_user(summary["user_id"])
                    count = summary["count"]
                    total = money(summary["total_cents"])
                    owner_ids = sorted(self.settings.owner_user_ids)
                    contact = f"<@{owner_ids[0]}>" if owner_ids else "the developer"
                    panel = Panel(
                        title=f"{E['money']} Payment Required",
                        body=(
                            "Your nightly checkout balance is **due now**.\n\n"
                            f"**Unpaid checkouts:** {count}\n"
                            f"**Rate:** {money(CHECKOUT_FEE_CENTS)} each\n"
                            f"### Amount Due: {total}\n\n"
                            f"Contact {contact} now to pay and clear this balance."
                        ),
                        color=C["error"],
                        timeout=None,
                        owner_id=summary["user_id"],
                    )
                    panel.footer("This payment reminder repeats nightly until the balance is cleared.")
                    await user.send(view=panel)
                    self.billing_store.mark_reminded(summary["job_ids"])
                    logger.info("Sent nightly payment demand to user %s", summary["user_id"])
                    self.queue_event_log(
                        "Nightly payment demand sent",
                        level="success",
                        user_id=summary["user_id"],
                        details={
                            "unpaid_checkouts": count,
                            "amount_due": total,
                        },
                    )
                except Exception as exc:
                    logger.warning("Nightly payment-demand DM failed for user %s: %s", summary["user_id"], exc)
                    self.queue_error_log(
                        "Nightly payment-demand DM",
                        exc,
                        user_id=summary["user_id"],
                    )
            await asyncio.sleep(60)

    async def setup_hook(self) -> None:
        load_custom_emojis()
        await sync_app_emojis(self)

        self.tree.clear_commands(guild=None)
        if self.settings.discord_guild_id:
            guild = discord.Object(id=self.settings.discord_guild_id)
            self.tree.clear_commands(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                logger.info("Cleared guild slash commands (%s remaining)", len(synced))
            except discord.Forbidden as exc:
                logger.warning("Could not clear slash commands in guild %s", self.settings.discord_guild_id)
                self.queue_error_log("Clear guild slash commands", exc)
        try:
            synced = await self.tree.sync()
            logger.info("Cleared global slash commands (%s remaining)", len(synced))
        except discord.HTTPException as exc:
            logger.warning("Could not clear global slash commands: %s", exc)
            self.queue_error_log("Clear global slash commands", exc)

    async def on_ready(self) -> None:
        logger.info("ONLINE — %s", self.user)
        self.queue_event_log(
            "Bot online",
            level="success",
            details={
                "bot_user_id": getattr(self.user, "id", "unknown"),
                "guild_id": self.settings.discord_guild_id,
                "restored_sessions": len(self.sessions),
                "whitelisted_users": len(self.settings.allowed_user_ids),
                "tracker_url": self.settings.tracker_base_url,
                "encrypted_tracking": True,
            },
        )
        now = int(datetime.now(timezone.utc).timestamp())
        expired_users = {
            user_id
            for user_id, expires_at in self.settings.allowed_user_expirations.items()
            if expires_at <= now
        }
        if expired_users:
            self.settings.allowed_user_ids.difference_update(expired_users)
            for user_id in expired_users:
                self.settings.allowed_user_expirations.pop(user_id, None)
            try:
                self.settings.save_runtime()
            except OSError as exc:
                self.queue_error_log("Prune expired whitelist access", exc)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="!start"))
        if self.success_monitor_task is None or self.success_monitor_task.done():
            self.success_monitor_task = asyncio.create_task(self.monitor_checkout_successes(), name="checkout-success-monitor")
        if self.nightly_billing_task is None or self.nightly_billing_task.done():
            self.nightly_billing_task = asyncio.create_task(self.send_nightly_billing_reminders(), name="nightly-billing")

    async def close(self) -> None:
        for task in (self.success_monitor_task, self.nightly_billing_task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await self.api.close()
        await super().close()

    def can_start(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        if user_id not in self.settings.allowed_user_ids:
            return False
        expires_at = self.settings.allowed_user_expirations.get(user_id)
        if not expires_at or expires_at > int(datetime.now(timezone.utc).timestamp()):
            return True
        self.settings.allowed_user_ids.discard(user_id)
        self.settings.allowed_user_expirations.pop(user_id, None)
        try:
            self.settings.save_runtime()
        except OSError as exc:
            self.queue_error_log("Expire whitelist access", exc, user_id=user_id)
        return False

    async def handle_whitelist(self, message: discord.Message, argument: str) -> None:
        await self.clean_input_message(message)
        if not self.is_owner(message.author.id):
            await message.channel.send(view=notice(
                f"{E['lock']} Admin Only",
                "Only the bot administrator can manage access.",
                C["error"],
            ))
            return
        try:
            parts = argument.split(maxsplit=1)
            if not parts:
                raise ValueError("Missing user ID")
            user_id = parse_discord_user_id(parts[0])
            duration_seconds = parse_whitelist_duration(parts[1] if len(parts) > 1 else "")
        except ValueError:
            await message.channel.send(view=notice(
                f"{E['warning']} Check Your Format",
                "Use `!whitelist USER_ID 7d`. Durations can use `m`, `h`, `d`, or `w`. Omit it for permanent access.",
                C["warning"],
            ))
            return

        already_allowed = user_id in self.settings.allowed_user_ids
        previous_expiration = self.settings.allowed_user_expirations.get(user_id)
        self.settings.allowed_user_ids.add(user_id)
        expires_at = (
            int(datetime.now(timezone.utc).timestamp()) + duration_seconds
            if duration_seconds is not None
            else None
        )
        if expires_at is None:
            self.settings.allowed_user_expirations.pop(user_id, None)
        else:
            self.settings.allowed_user_expirations[user_id] = expires_at
        try:
            self.settings.save_runtime()
        except OSError as exc:
            if not already_allowed:
                self.settings.allowed_user_ids.discard(user_id)
            if previous_expiration is None:
                self.settings.allowed_user_expirations.pop(user_id, None)
            else:
                self.settings.allowed_user_expirations[user_id] = previous_expiration
            self.queue_error_log("Save whitelist access", exc, user_id=user_id)
            await message.channel.send(view=notice(
                f"{E['cross']} Could Not Save Access",
                "Try the command again in a moment.",
                C["error"],
            ))
            return

        if expires_at is None:
            body = f"<@{user_id}> can now use `!start` permanently."
        else:
            body = f"<@{user_id}> can use `!start` until <t:{expires_at}:F> (<t:{expires_at}:R>)."
        await message.channel.send(view=notice(f"{E['check']} User Whitelisted", body, C["success"]))
        self.queue_event_log(
            "Whitelist granted",
            level="admin",
            user_id=user_id,
            details={
                "admin_user_id": message.author.id,
                "expires_at": expires_at or "permanent",
            },
        )

    async def handle_revoke(self, message: discord.Message, argument: str) -> None:
        await self.clean_input_message(message)
        if not self.is_owner(message.author.id):
            await message.channel.send(view=notice(
                f"{E['lock']} Admin Only",
                "Only the bot administrator can manage access.",
                C["error"],
            ))
            return
        try:
            user_id = parse_discord_user_id(argument)
        except ValueError:
            await message.channel.send(view=notice(
                f"{E['warning']} Check Your Format",
                "Use `!revoke USER_ID`.",
                C["warning"],
            ))
            return

        was_allowed = user_id in self.settings.allowed_user_ids
        previous_expiration = self.settings.allowed_user_expirations.get(user_id)
        self.settings.allowed_user_ids.discard(user_id)
        self.settings.allowed_user_expirations.pop(user_id, None)
        try:
            self.settings.save_runtime()
        except OSError as exc:
            if was_allowed:
                self.settings.allowed_user_ids.add(user_id)
            if previous_expiration is not None:
                self.settings.allowed_user_expirations[user_id] = previous_expiration
            self.queue_error_log("Save revoked whitelist access", exc, user_id=user_id)
            await message.channel.send(view=notice(
                f"{E['cross']} Could Not Save Access",
                "Try the command again in a moment.",
                C["error"],
            ))
            return

        if self.is_owner(user_id):
            body = f"Removed <@{user_id}> from the whitelist, but this user is still a configured administrator."
        elif was_allowed or previous_expiration is not None:
            body = f"<@{user_id}> can no longer use `!start`."
        else:
            body = f"<@{user_id}> was not whitelisted."
        await message.channel.send(view=notice(f"{E['check']} Access Revoked", body, C["success"]))
        self.queue_event_log(
            "Whitelist revoked",
            level="admin",
            user_id=user_id,
            details={
                "admin_user_id": message.author.id,
                "previously_allowed": was_allowed or previous_expiration is not None,
            },
        )

    async def handle_dashboard(self, message: discord.Message, argument: str) -> None:
        await self.clean_input_message(message)
        if argument:
            await message.channel.send(view=notice(
                f"{E['warning']} Check Your Format",
                "Use `!dashboard`.",
                C["warning"],
            ))
            return
        if not self.can_start(message.author.id):
            await message.channel.send(view=notice(
                f"{E['lock']} Access Required",
                "You are not whitelisted to use this bot.",
                C["error"],
            ))
            return

        dashboard_message = await message.channel.send(view=progress_panel("Opening Dashboard"))
        panel = await self.build_dashboard_panel(message.author.id)
        await dashboard_message.edit(view=panel)
        self.queue_event_log(
            "Dashboard opened",
            user_id=message.author.id,
            details={"screen": "verification settings"},
        )

    async def handle_payments(self, message: discord.Message, argument: str) -> None:
        await self.clean_input_message(message)
        if not self.is_owner(message.author.id):
            await message.channel.send(view=notice(
                f"{E['lock']} Admin Only",
                "Only the bot administrator can manage checkout balances.",
                C["error"],
            ))
            return

        parts = argument.split()
        action = parts[0].lower() if len(parts) == 2 else ""
        if len(parts) != 2 or action not in {"balance", "clear"}:
            await message.channel.send(view=notice(
                f"{E['warning']} Check Your Format",
                "Use `!payments balance USER_ID` or `!payments clear USER_ID`.",
                C["warning"],
            ))
            return
        try:
            user_id = parse_discord_user_id(parts[1])
        except ValueError:
            await message.channel.send(view=notice(
                f"{E['warning']} Check Your Format",
                "Use `!payments balance USER_ID` or `!payments clear USER_ID`.",
                C["warning"],
            ))
            return

        if action == "clear":
            cleared = self.billing_store.clear_balance(user_id)
            count = cleared["count"]
            total = money(cleared["total_cents"])
            body = (
                f"Cleared **{count} checkout{'s' if count != 1 else ''}** totaling **{total}** for <@{user_id}>."
                if count
                else f"<@{user_id}> had no outstanding balance to clear."
            )
            await message.channel.send(view=notice(
                f"{E['check']} Balance Cleared",
                body,
                C["success"],
            ))
            self.queue_event_log(
                "Checkout balance cleared",
                level="admin",
                user_id=user_id,
                details={
                    "admin_user_id": message.author.id,
                    "cleared_checkouts": count,
                    "cleared_total": total,
                },
            )
            return

        summary = self.billing_store.balance_summary(user_id)
        count = summary["count"]
        total = money(summary["total_cents"])
        self.queue_event_log(
            "Checkout balance viewed",
            level="admin",
            user_id=user_id,
            details={
                "admin_user_id": message.author.id,
                "unpaid_checkouts": count,
                "amount_due": total,
            },
        )
        if not count:
            await message.channel.send(view=notice(
                f"{E['money']} Balance",
                f"<@{user_id}> has no outstanding balance.",
                C["success"],
            ))
            return

        panel = Panel(
            title=f"{E['money']} Outstanding Balance",
            body=(
                f"<@{user_id}> has **{count} unpaid checkout{'s' if count != 1 else ''}**.\n\n"
                f"### Total Owed: {total}"
            ),
            color=C["warning"],
            timeout=None,
        )
        panel.rule()
        panel.line(f"**Checkout fee:** {money(CHECKOUT_FEE_CENTS)} each")
        panel.footer("Owner-only balance lookup")
        await message.channel.send(view=panel)

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        content = message.content.strip()
        parts = content.split(maxsplit=1)
        command = parts[0].lower() if parts else ""
        argument = parts[1].strip() if len(parts) > 1 else ""
        if command == "!whitelist":
            await self.handle_whitelist(message, argument)
            return
        if command == "!revoke":
            await self.handle_revoke(message, argument)
            return
        if command == "!payments":
            await self.handle_payments(message, argument)
            return
        if command == "!dashboard":
            await self.handle_dashboard(message, argument)
            return
        if command != "!start" or argument:
            return
        if not self.can_start(message.author.id):
            self.queue_event_log(
                "Start command denied",
                level="warning",
                user_id=message.author.id,
                details={"reason": "not whitelisted"},
            )
            await self.clean_input_message(message)
            await message.channel.send(view=notice(
                f"{E['lock']} Access Required",
                "You are not whitelisted to use this bot.",
                C["error"],
            ))
            return
        self.queue_event_log(
            "Start command accepted",
            user_id=message.author.id,
            details={
                "guild_id": getattr(message.guild, "id", None) or "direct_message",
                "reopened_active_order": False,
            },
        )
        if self.settings.maintenance and not self.is_owner(message.author.id):
            panel = notice(f"{E['warning']} Temporarily Unavailable", self.settings.maintenance_message, C["warning"])
            main = await message.channel.send(view=panel)
            self.main_messages[message.author.id] = main
            return
        if message.author.id in self.collecting_users:
            return

        active = self.sessions.get(message.author.id)
        if active and active.job_id and active.state in {FlowState.BUILDING_DRAFT, FlowState.DRAFT_READY, FlowState.PLACING}:
            self.queue_event_log(
                "Active order reopened",
                user_id=message.author.id,
                job_id=active.job_id,
                details={"state": active.state.value},
            )
            try:
                job = await self.api.get_job(active.job_id)
                active.job = job
                await self.maybe_announce_success(active, job)
                panel = self.panel_for_job(active, job)
                self.set_session(active)
            except Exception as exc:
                self.queue_error_log(
                    "Reopen active order",
                    exc,
                    user_id=message.author.id,
                    job_id=active.job_id,
                )
                panel = api_error_panel("Could Not Load Order", exc)
            if not await self.edit_main_panel(message.author.id, panel):
                main = await message.channel.send(view=panel)
                self.main_messages[message.author.id] = main
            return

        self.collecting_users.add(message.author.id)

        async def collect() -> None:
            try:
                await self.start_message_flow(message)
            finally:
                self.collecting_users.discard(message.author.id)

        asyncio.create_task(collect(), name=f"start-flow-{message.author.id}")

    async def wait_for_order_input(self, command: discord.Message) -> discord.Message:
        return await self.wait_for(
            "message",
            timeout=300,
            check=lambda candidate: (
                not candidate.author.bot
                and candidate.author.id == command.author.id
                and candidate.channel.id == command.channel.id
                and bool(candidate.content.strip())
            ),
        )

    @staticmethod
    async def clean_input_message(message: discord.Message) -> None:
        if message.guild is not None:
            with contextlib.suppress(discord.Forbidden, discord.NotFound, discord.HTTPException):
                await message.delete()

    async def start_message_flow(self, command: discord.Message) -> None:
        user_id = command.author.id
        await self.clean_input_message(command)
        panel = Panel(
            title=f"{E['link']} Start Your Order",
            body=(
                "Send the **group link and full delivery address in one line**:\n\n"
                "`GROUP LINK, FULL ADDRESS`"
            ),
            color=C["primary"],
            timeout=None,
            owner_id=user_id,
        )
        panel.footer("You will add the card after your order is priced.")
        main = await command.channel.send(view=panel)
        self.main_messages[user_id] = main

        try:
            order_message = await self.wait_for_order_input(command)
            order_line = order_message.content.strip()
            await self.clean_input_message(order_message)
            if order_line.lower() in {"cancel", "stop", "quit"}:
                self.queue_event_log(
                    "Order setup cancelled",
                    level="warning",
                    user_id=user_id,
                    details={"stage": "waiting_for_order_details"},
                )
                await main.edit(view=notice(f"{E['cross']} Cancelled", "Send `!start` whenever you are ready.", C["error"]))
                return
            try:
                group_cart, address = parse_start_line(order_line)
            except ValueError:
                self.queue_event_log(
                    "Order input rejected",
                    level="warning",
                    user_id=user_id,
                    details={"reason": "invalid link or address format"},
                )
                await main.edit(view=notice(
                    f"{E['cross']} Check Your Format",
                    "Send `!start` again, then use `GROUP LINK, FULL ADDRESS` on one line.",
                    C["error"],
                ))
                return

            session = OrderSession(
                user_id=user_id,
                group_cart=group_cart,
                address=address,
                state=FlowState.BUILDING_DRAFT,
            )
            self.set_session(session)
            await self.build_draft_from_message(main, session)
        except asyncio.TimeoutError:
            self.queue_event_log(
                "Order setup timed out",
                level="warning",
                user_id=user_id,
                details={"timeout_seconds": 300},
            )
            await main.edit(view=notice(f"{E['clock']} Session Expired", "Send `!start` to begin again.", C["warning"]))

    async def build_draft_from_message(self, main: discord.Message, session: OrderSession) -> None:
        if self.lock_for(session.user_id).locked():
            await main.edit(view=notice(f"{E['warning']} Order In Progress", "Your current order is still being prepared.", C["warning"]))
            return
        async with self.lock_for(session.user_id):
            await main.edit(view=progress_panel("Setting Up Order"))
            try:
                created = await self.api.create_job(session.create_payload())
                session.job_id = str(created.get("job_id") or "")
                if not session.job_id:
                    raise WoolixApiError(502, {"error": "No order reference returned"})
                session.card = None
                session.state = FlowState.BUILDING_DRAFT
                self.set_session(session)

                async def update(job: dict[str, Any]) -> None:
                    session.job = job
                    await main.edit(view=progress_panel("Setting Up Order", job))

                job = await self.api.wait_for_draft(
                    session.job_id,
                    timeout_seconds=self.settings.draft_timeout_seconds,
                    interval_seconds=self.settings.poll_interval_seconds,
                    on_update=update,
                    awaiting_config_done=True,
                )
                job = await self.repair_suspected_duplicate(
                    session,
                    job,
                    on_update=update,
                    awaiting_config_done=True,
                )
                session.job = job
                await hold_setup_transition(job)
                panel = self.panel_for_job(session, job)
                self.set_session(session)
                await main.edit(view=panel)
            except Exception as exc:
                # Never retain full card details after the initial submission attempt.
                session.card = None
                self.queue_error_log(
                    "Create priced draft",
                    exc,
                    user_id=session.user_id,
                    job_id=session.job_id,
                )
                if isinstance(exc, WoolixApiError) and exc.status == 0 and session.job_id:
                    session.state = FlowState.BUILDING_DRAFT
                    self.set_session(session)
                    await main.edit(view=progress_panel("Order Is Processing", session.job))
                else:
                    session.state = FlowState.FAILED
                    self.set_session(session)
                    await render_setup_failure(main.edit, exc)

    async def build_draft(self, interaction: discord.Interaction, session: OrderSession) -> None:
        if not session.card:
            await interaction.response.send_message("Add a card before building the draft.", ephemeral=True)
            return
        if self.lock_for(session.user_id).locked():
            await interaction.response.send_message("This order is already processing.", ephemeral=True)
            return
        async with self.lock_for(session.user_id):
            if interaction.message is None:
                await interaction.response.send_message(view=progress_panel("Setting Up Order"), ephemeral=True)
            else:
                await interaction.response.edit_message(view=progress_panel("Setting Up Order"))
            self.remember_main_interaction(interaction)
            try:
                created = await self.api.create_job(session.create_payload())
                session.job_id = str(created.get("job_id") or "")
                if not session.job_id:
                    raise WoolixApiError(502, {"error": "The ordering service did not return a job ID"})
                session.card = None
                session.state = FlowState.BUILDING_DRAFT
                self.set_session(session)
                job = await self.wait_for_draft(interaction, session)
                await hold_setup_transition(job)
                panel = self.panel_for_job(session, job)
                self.set_session(session)
                await interaction.edit_original_response(view=panel)
            except Exception as exc:
                self.queue_error_log(
                    "Create priced draft from interaction",
                    exc,
                    user_id=session.user_id,
                    job_id=session.job_id,
                )
                if isinstance(exc, WoolixApiError) and exc.status == 0 and session.job_id:
                    session.state = FlowState.BUILDING_DRAFT
                    self.set_session(session)
                    await interaction.edit_original_response(view=progress_panel("Order Is Processing", session.job))
                else:
                    session.state = FlowState.FAILED
                    self.set_session(session)
                    await render_setup_failure(interaction.edit_original_response, exc)

    async def wait_for_draft(self, interaction: discord.Interaction, session: OrderSession) -> dict[str, Any]:
        async def update(job: dict[str, Any]) -> None:
            await interaction.edit_original_response(view=progress_panel("Setting Up Order", job))

        job = await self.api.wait_for_draft(
            session.job_id,
            timeout_seconds=self.settings.draft_timeout_seconds,
            interval_seconds=self.settings.poll_interval_seconds,
            on_update=update,
        )
        job = await self.repair_suspected_duplicate(
            session,
            job,
            on_update=update,
        )
        session.job = job
        session.state = (
            FlowState.DRAFT_READY
            if job.get("status") == "draft_ready" or job.get("card_error")
            else FlowState.FAILED
        )
        self.set_session(session)
        return job

    async def repair_suspected_duplicate(
        self,
        session: OrderSession,
        job: dict[str, Any],
        *,
        on_update,
        awaiting_config_done: bool = False,
    ) -> dict[str, Any]:
        """Rebuild once when a subtotal contains exactly one hidden item price."""
        extra_cents = suspected_duplicate_item_cents(job)
        if not extra_cents or session.integrity_rebuild_attempted:
            return job

        session.integrity_rebuild_attempted = True
        session.job = job
        self.set_session(session)
        self.queue_event_log(
            "Hidden duplicate item detected",
            level="warning",
            user_id=session.user_id,
            job_id=session.job_id,
            details={
                "extra_amount": money(extra_cents),
                "action": "automatic cart rebuild",
            },
        )
        await on_update({"status": "cart_syncing", "substatus": "Rechecking cart total"})
        old_job_id = session.job_id
        new_job_id = ""
        try:
            rebuilt = await self.api.rebuild_job(old_job_id, session.group_cart)
            new_job_id = str(rebuilt.get("new_job_id") or "").strip()
            if not new_job_id:
                raise WoolixApiError(502, {"error": "No replacement order reference returned"})
            session.job_id = new_job_id
            session.state = FlowState.BUILDING_DRAFT
            self.set_session(session)
            repaired = await self.api.wait_for_draft(
                new_job_id,
                timeout_seconds=self.settings.draft_timeout_seconds,
                interval_seconds=self.settings.poll_interval_seconds,
                on_update=on_update,
                awaiting_config_done=awaiting_config_done,
            )
            remaining_extra = suspected_duplicate_item_cents(repaired)
            self.queue_event_log(
                "Automatic cart rebuild completed",
                level="warning" if remaining_extra else "success",
                user_id=session.user_id,
                job_id=new_job_id,
                details={
                    "previous_job_id": old_job_id,
                    "cart_total_verified": not bool(remaining_extra),
                },
            )
            return repaired
        except Exception as exc:
            self.queue_error_log(
                "Automatic duplicate-cart rebuild",
                exc,
                user_id=session.user_id,
                job_id=session.job_id,
            )
            # Keep checkout blocked by CartMismatchPanel if repair could not be verified.
            session.job_id = new_job_id or old_job_id
            session.job = job
            session.state = FlowState.DRAFT_READY
            self.set_session(session)
            return job

    async def update_configuration(
        self,
        interaction: discord.Interaction,
        session: OrderSession,
        changes: dict[str, Any],
        title: str,
    ) -> None:
        await interaction.response.edit_message(view=progress_panel(title, session.job))
        try:
            job = await self.api.configure_job(session.job_id, changes)
            session.job = job
            self.set_session(session)
            await interaction.edit_original_response(view=self.panel_for_job(session, job))
            self.queue_event_log(
                "Order configuration updated",
                user_id=session.user_id,
                job_id=session.job_id,
                details={"action": title},
            )
        except Exception as exc:
            self.queue_error_log(
                "Update order configuration",
                exc,
                user_id=session.user_id,
                job_id=session.job_id,
            )
            await interaction.edit_original_response(view=api_error_panel("Could Not Update Order", exc))

    async def update_job_action(
        self,
        interaction: discord.Interaction,
        session: OrderSession,
        title: str,
        action,
        value: str,
        *,
        fulfillment: str | None = None,
    ) -> None:
        await interaction.response.edit_message(view=progress_panel(title, session.job))
        try:
            job = await action(session.job_id, value)
            if fulfillment:
                session.fulfillment = fulfillment
            session.job = job
            self.set_session(session)
            await interaction.edit_original_response(view=self.panel_for_job(session, job))
            self.queue_event_log(
                "Order option updated",
                user_id=session.user_id,
                job_id=session.job_id,
                details={
                    "action": title,
                    "fulfillment": fulfillment or session.fulfillment,
                },
            )
        except Exception as exc:
            self.queue_error_log(
                "Update order action",
                exc,
                user_id=session.user_id,
                job_id=session.job_id,
            )
            await interaction.edit_original_response(view=api_error_panel("Could Not Update Order", exc))

    async def place_order(self, interaction: discord.Interaction, session: OrderSession) -> None:
        active = self.sessions.get(session.user_id)
        if active and active.job_id and active.job_id != session.job_id:
            await interaction.response.send_message(
                "This is an old checkout panel. Send `!start` to open your current order.",
                ephemeral=True,
            )
            return
        if active is not None:
            session = active
        if session.state in {FlowState.PLACING, FlowState.PLACED} or session.success_announced:
            await interaction.response.send_message("This order is already processing or completed.", ephemeral=True)
            return
        if self.lock_for(session.user_id).locked():
            await interaction.response.send_message("This order is already processing.", ephemeral=True)
            return
        async with self.lock_for(session.user_id):
            active = self.sessions.get(session.user_id)
            if active and active.job_id and active.job_id != session.job_id:
                await interaction.response.send_message(
                    "This is an old checkout panel. Send `!start` to open your current order.",
                    ephemeral=True,
                )
                return
            if active is not None:
                session = active
            if session.state in {FlowState.PLACING, FlowState.PLACED} or session.success_announced:
                await interaction.response.send_message("This order is already processing or completed.", ephemeral=True)
                return
            self.remember_main_interaction(interaction)
            await interaction.response.edit_message(view=progress_panel("Checking Order", session.job))
            submitted = False
            proceed_started = False
            try:
                latest = await self.api.get_job(session.job_id)
                session.job = latest
                status = str(latest.get("status") or "")
                payment = str(latest.get("payment_status") or "")
                if (
                    status in {"placing", "placed"}
                    or payment in SETTLED_PAYMENT_STATUSES | {"verifying", "verifying_long"}
                    or bool(latest.get("order_uuid"))
                ):
                    await self.maybe_announce_success(session, latest)
                    panel = self.panel_for_job(session, latest)
                    self.set_session(session)
                    await interaction.edit_original_response(view=panel)
                    return
                if status != "draft_ready":
                    panel = self.panel_for_job(session, latest)
                    self.set_session(session)
                    await interaction.edit_original_response(view=panel)
                    return

                extra_cents = suspected_duplicate_item_cents(latest)
                if extra_cents:
                    session.state = FlowState.DRAFT_READY
                    self.set_session(session)
                    self.queue_event_log(
                        "Checkout blocked for hidden duplicate item",
                        level="warning",
                        user_id=session.user_id,
                        job_id=session.job_id,
                        details={"extra_amount": money(extra_cents)},
                    )
                    await interaction.edit_original_response(view=CartMismatchPanel(self, session, latest))
                    return

                session.state = FlowState.PLACING
                self.set_session(session)
                await interaction.edit_original_response(view=progress_panel("Placing Order", {"status": "placing"}))
                proceed_started = True
                job = await self.api.proceed_job(session.job_id)
                submitted = True
                session.job = job
                self.set_session(session)
                if job.get("payment_status") in {"verifying", "verifying_long"}:
                    async def update(latest: dict[str, Any]) -> None:
                        await interaction.edit_original_response(view=progress_panel("Verifying Payment", latest))

                    job = await self.api.wait_for_settlement(
                        session.job_id,
                        timeout_seconds=self.settings.settlement_timeout_seconds,
                        interval_seconds=self.settings.poll_interval_seconds,
                        on_update=update,
                    )
                    session.job = job
                    self.set_session(session)
                await self.maybe_announce_success(session, job)
                panel = self.panel_for_job(session, job)
                self.set_session(session)
                await interaction.edit_original_response(view=panel)
            except Exception as exc:
                self.queue_error_log(
                    "Place order",
                    exc,
                    user_id=session.user_id,
                    job_id=session.job_id,
                )
                uncertain_submission = (
                    proceed_started
                    and isinstance(exc, WoolixApiError)
                    and exc.status == 0
                )
                if (submitted or uncertain_submission) and not is_credit_exhausted(exc):
                    session.state = FlowState.PLACING
                    self.set_session(session)
                    await interaction.edit_original_response(view=UnverifiedPanel(self, session, session.job))
                else:
                    session.state = FlowState.FAILED
                    self.set_session(session)
                    await interaction.edit_original_response(view=api_error_panel("Checkout Failed", exc))

    async def cancel_session(self, interaction: discord.Interaction, session: OrderSession) -> None:
        active = self.sessions.get(session.user_id)
        if active is not None and active.job_id != session.job_id:
            await interaction.response.send_message(
                "This is an old checkout panel. Send `!start` to open your current order.",
                ephemeral=True,
            )
            return
        if active is not None:
            session = active
        if session.state == FlowState.PLACING:
            await interaction.response.send_message("The order cannot be cancelled while placement is in progress.", ephemeral=True)
            return
        if interaction.response.is_done():
            editor = interaction.edit_original_response
        else:
            if interaction.message is None:
                await interaction.response.send_message(view=progress_panel("Cancelling Order"), ephemeral=True)
            else:
                await interaction.response.edit_message(view=progress_panel("Cancelling Order"))
            editor = interaction.edit_original_response
        try:
            if session.job_id and session.state not in {FlowState.PLACED, FlowState.CANCELLED}:
                try:
                    await self.api.cancel_job(session.job_id)
                except WoolixApiError as cancel_error:
                    if cancel_error.status not in {404, 409}:
                        raise
                    latest: dict[str, Any] | None = None
                    if cancel_error.status == 409:
                        try:
                            latest = await self.api.get_job(session.job_id)
                        except WoolixApiError as lookup_error:
                            if lookup_error.status != 404:
                                raise cancel_error
                    if latest:
                        status = str(latest.get("status") or "").lower()
                        payment = str(latest.get("payment_status") or "").lower()
                        committed = (
                            status in {"placing", "placed"}
                            or payment in {"succeeded", "verifying", "verifying_long", "unverified"}
                            or bool(latest.get("order_uuid"))
                        )
                        if committed:
                            session.job = latest
                            panel = self.panel_for_job(session, latest)
                            self.set_session(session)
                            await editor(view=panel)
                            return
            session.state = FlowState.CANCELLED
            if not self.clear_session(session.user_id, expected_job_id=session.job_id):
                await editor(view=notice(
                    f"{E['warning']} Checkout Changed",
                    "This panel belongs to an older checkout. Send `!start` to open your current order.",
                    C["warning"],
                ))
                return
            await editor(view=notice(f"{E['cross']} Cancelled", "Send `!start` to begin.", C["error"]))
            self.queue_event_log(
                "Order cancelled",
                level="warning",
                user_id=session.user_id,
                job_id=session.job_id,
            )
        except Exception as exc:
            self.queue_error_log(
                "Cancel order",
                exc,
                user_id=session.user_id,
                job_id=session.job_id,
            )
            await editor(view=api_error_panel("Could Not Cancel Order", exc))

    def panel_for_job(self, session: OrderSession, job: dict[str, Any]) -> Panel:
        cart = job.get("cart") if isinstance(job.get("cart"), dict) else {}
        store_name = str(cart.get("store_name") or "").strip()
        if store_name:
            session.store_name = store_name[:100]
        session.job = job
        self.queue_job_error_log(session, job)
        status = str(job.get("status") or "")
        payment = str(job.get("payment_status") or "")
        if is_credit_exhausted(job):
            session.state = FlowState.FAILED
            return setup_connection_panel()
        if job.get("card_error"):
            session.state = FlowState.DRAFT_READY
            return CardRetryPanel(self, session, job)
        if status == "draft_ready" and job.get("place_order_error"):
            session.state = FlowState.DRAFT_READY
            return RetryOrderPanel(self, session, job)
        if status in {"draft_ready", "awaiting_config"}:
            session.state = FlowState.DRAFT_READY
            if suspected_duplicate_item_cents(job):
                return CartMismatchPanel(self, session, job)
            return DraftPanel(self, session, job)
        if payment == "succeeded":
            session.state = FlowState.PLACED
            return OrderPlacedPanel(self, session, job)
        if payment == "unverified":
            session.state = FlowState.PLACING
            return UnverifiedPanel(self, session, job)
        if payment in {"declined", "failed"} or job.get("card_error"):
            session.state = FlowState.FAILED
            return CardRetryPanel(self, session, job) if job.get("card_error") else RetryOrderPanel(self, session, job)
        if status in {"failed", "expired", "cancelled"} or job.get("error"):
            session.state = FlowState.FAILED if status != "cancelled" else FlowState.CANCELLED
            return RetryOrderPanel(self, session, job)
        if payment in SETTLED_PAYMENT_STATUSES and order_tracker_url(job):
            session.state = FlowState.PLACED
            return OrderPlacedPanel(self, session, job)
        return progress_panel("Order Is Processing", job)


async def run_bot(settings: Settings | None = None) -> None:
    resolved = settings or Settings.from_file()
    bot = WoolixBot(resolved)
    await bot.start(resolved.discord_token)
