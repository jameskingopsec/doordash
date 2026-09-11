"""The same red Components V2 panel system used by direct-aco-new."""

from __future__ import annotations

import json
import logging
import re

import discord

from .config import DATA_DIR
from .formatting import friendly_error_details


logger = logging.getLogger(__name__)

C = {
    "primary": 0xE31837,
    "success": 0x57F287,
    "error": 0xED4245,
    "warning": 0xFEE75C,
    "info": 0x5865F2,
    "neutral": 0x2B2D31,
}

E = {
    "dominos": "🍕", "card": "💳", "cash": "💵", "cart": "🛒", "check": "✅",
    "cross": "❌", "loading": "⏳", "rocket": "🚀", "warning": "⚠️",
    "sparkles": "✨", "receipt": "🧾", "pin": "📍", "phone": "📱", "mail": "📧",
    "person": "👤", "lock": "🔒", "store": "🏪", "note": "📝", "back": "⬅️",
    "trash": "🗑️", "plus": "➕", "clock": "🕒", "menu": "📋", "camera": "📸",
    "money": "💰", "link": "🔗", "refresh": "🔄", "close": "✖️", "star": "⭐",
    "car": "🚗", "deliver": "🛍️", "done": "🎉", "info": "ℹ️", "tip": "💸",
}

EMOJI_FILE = DATA_DIR / "emojis.json"
ASSET_DIR = DATA_DIR / "emoji_assets"
_ASSET_EXTENSIONS = (".gif", ".png", ".jpg", ".jpeg", ".webp")
_CUSTOM_EMOJI = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{15,25}>$")
_EMOJI_PREFIX = "aco_"


def load_custom_emojis() -> dict[str, str]:
    if not EMOJI_FILE.exists():
        return {}
    try:
        raw = json.loads(EMOJI_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Could not read %s: %s", EMOJI_FILE, exc)
        return {}
    if not isinstance(raw, dict):
        return {}
    applied = {str(key): str(value).strip() for key, value in raw.items() if str(value).strip()}
    E.update(applied)
    return applied


async def sync_app_emojis(bot: discord.Client) -> dict[str, str]:
    """Upload/reuse the bundled emoji assets as Discord application emojis."""
    applied: dict[str, str] = {}
    try:
        existing = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}
    except Exception as exc:
        logger.warning("Could not list application emojis: %s", exc)
        reporter = getattr(bot, "queue_error_log", None)
        if callable(reporter):
            reporter("List application emojis", exc)
        return applied

    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    assets = sorted(
        path for path in ASSET_DIR.iterdir()
        if path.is_file() and path.suffix.lower() in _ASSET_EXTENSIONS
    )
    for path in assets:
        key = re.sub(r"[^a-z0-9_]", "_", path.stem.lower())
        name = f"{_EMOJI_PREFIX}{key}"[:32]
        emoji = existing.get(name)
        if emoji is None:
            try:
                data = path.read_bytes()
                if len(data) > 256_000:
                    logger.warning("Skipping %s: over Discord's 256KB emoji limit", path.name)
                    continue
                emoji = await bot.create_application_emoji(name=name, image=data)
                existing[name] = emoji
                logger.info("Uploaded application emoji :%s:", name)
            except Exception as exc:
                logger.warning("Could not upload %s: %s", path.name, exc)
                reporter = getattr(bot, "queue_error_log", None)
                if callable(reporter):
                    reporter(f"Upload application emoji {path.name}", exc)
                continue
        applied[key] = str(emoji)

    for name, emoji in existing.items():
        if name.startswith(_EMOJI_PREFIX):
            applied.setdefault(name[len(_EMOJI_PREFIX):], str(emoji))
    E.update(applied)
    logger.info("Using %s custom application emojis", len(applied))
    return applied


def as_partial(key_or_emoji: str | None):
    if not key_or_emoji:
        return None
    value = E.get(key_or_emoji, key_or_emoji)
    if _CUSTOM_EMOJI.match(value):
        return discord.PartialEmoji.from_str(value)
    return value


def text(content: str) -> discord.ui.TextDisplay:
    return discord.ui.TextDisplay(content)


def divider(large: bool = False) -> discord.ui.Separator:
    spacing = discord.SeparatorSpacing.large if large else discord.SeparatorSpacing.small
    return discord.ui.Separator(spacing=spacing)


def button(
    label: str,
    callback,
    *,
    style: discord.ButtonStyle = discord.ButtonStyle.secondary,
    emoji: str | None = None,
    disabled: bool = False,
) -> discord.ui.Button:
    item = discord.ui.Button(
        label=label,
        style=style,
        emoji=as_partial(emoji),
        disabled=disabled,
    )
    item.callback = callback
    return item


def link_button(label: str, url: str, *, emoji: str | None = None) -> discord.ui.Button:
    return discord.ui.Button(label=label, url=url, emoji=as_partial(emoji))


def disable_all(view: discord.ui.LayoutView | discord.ui.View) -> None:
    children = view.walk_children() if hasattr(view, "walk_children") else view.children
    for child in children:
        if hasattr(child, "disabled"):
            try:
                child.disabled = True
            except AttributeError:
                pass


class Panel(discord.ui.LayoutView):
    """One accent-coloured Components V2 container."""

    def __init__(
        self,
        *,
        title: str = "",
        body: str = "",
        color: int = C["primary"],
        timeout: float | None = 600,
        owner_id: int | None = None,
    ):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.container = discord.ui.Container(accent_colour=discord.Colour(color))
        self.add_item(self.container)
        if title:
            self.container.add_item(text(f"## {title}"))
        if body:
            self.container.add_item(text(body))

    def line(self, content: str) -> "Panel":
        if content:
            self.container.add_item(text(content))
        return self

    def rule(self, large: bool = False) -> "Panel":
        self.container.add_item(divider(large))
        return self

    def footer(self, content: str) -> "Panel":
        if content:
            self.container.add_item(divider())
            self.container.add_item(text(f"-# {content}"))
        return self

    def row(self, *items: discord.ui.Item) -> "Panel":
        present = [item for item in items if item is not None]
        if present:
            self.container.add_item(discord.ui.ActionRow(*present))
        return self

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.owner_id is None or interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            f"{E['lock']} This checkout belongs to someone else. Send `!start` for your own.",
            ephemeral=True,
        )
        return False

    async def on_error(self, interaction: discord.Interaction, error: Exception, _item) -> None:
        logger.exception("UI callback failed", exc_info=error)
        client = interaction.client
        reporter = getattr(client, "queue_error_log", None)
        if callable(reporter):
            session = getattr(client, "sessions", {}).get(interaction.user.id)
            reporter(
                "Discord UI callback",
                error,
                user_id=interaction.user.id,
                job_id=getattr(session, "job_id", ""),
            )
        title, detail = friendly_error_details(error)
        message = f"{E['cross']} **{title}**\n{detail}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            pass


def notice(title: str, body: str = "", color: int = C["info"], footer: str = "") -> Panel:
    panel = Panel(title=title, body=body, color=color, timeout=None)
    if footer:
        panel.footer(footer)
    return panel
