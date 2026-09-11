"""Tracked JSON configuration for the bot and its admin controls."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def _positive_float(name: str, value: Any, default: float) -> float:
    try:
        parsed = float(default if value is None else value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return parsed


def _ids(name: str, raw: Any) -> set[int]:
    values: set[int] = set()
    if raw is None:
        return values
    if not isinstance(raw, list):
        raise RuntimeError(f"{name} must be a JSON array of Discord IDs")
    for part in raw:
        try:
            values.add(int(str(part).strip()))
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain Discord IDs") from exc
    return values


def _expirations(name: str, raw: Any) -> dict[int, int]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RuntimeError(f"{name} must be a JSON object of user IDs and Unix timestamps")
    values: dict[int, int] = {}
    for user_id, expires_at in raw.items():
        try:
            parsed_user_id = int(str(user_id).strip())
            parsed_expiration = int(str(expires_at).strip())
        except ValueError as exc:
            raise RuntimeError(f"{name} must contain Discord IDs and Unix timestamps") from exc
        if parsed_user_id <= 0 or parsed_expiration <= 0:
            raise RuntimeError(f"{name} values must be positive")
        values[parsed_user_id] = parsed_expiration
    return values


@dataclass(slots=True)
class Settings:
    discord_token: str
    woolix_api_key: str
    woolix_api_base_url: str
    discord_guild_id: int | None
    log_channel_id: int | None
    owner_user_ids: set[int]
    allowed_user_ids: set[int]
    allowed_user_expirations: dict[int, int]
    api_timeout_seconds: float
    draft_timeout_seconds: float
    settlement_timeout_seconds: float
    poll_interval_seconds: float
    bot_name: str
    maintenance: bool
    maintenance_message: str
    success_webhook_url: str
    database_url: str
    tracker_base_url: str
    tracker_slug_secret: str
    config_path: Path

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Settings":
        target = path or ROOT / "config.json"
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RuntimeError(f"Missing {target.name}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Could not read {target}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("config.json must contain a JSON object")

        discord_config = raw.get("discord") if isinstance(raw.get("discord"), dict) else {}
        woolix_config = raw.get("woolix") if isinstance(raw.get("woolix"), dict) else {}
        bot_config = raw.get("bot") if isinstance(raw.get("bot"), dict) else {}
        database_config = raw.get("database") if isinstance(raw.get("database"), dict) else {}
        tracking_config = raw.get("tracking") if isinstance(raw.get("tracking"), dict) else {}
        discord_token = str(discord_config.get("token") or "").strip()
        api_key = str(woolix_config.get("api_key") or "").strip()
        missing = []
        if not discord_token:
            missing.append("discord.token")
        if not api_key:
            missing.append("woolix.api_key")
        if missing:
            raise RuntimeError(f"Missing required config.json values: {', '.join(missing)}")

        guild_raw = discord_config.get("guild_id")
        log_channel_raw = discord_config.get("log_channel_id")
        try:
            guild_id = int(str(guild_raw).strip()) if guild_raw not in (None, "") else None
            log_channel_id = int(str(log_channel_raw).strip()) if log_channel_raw not in (None, "") else None
        except ValueError as exc:
            raise RuntimeError("discord guild/log channel IDs must be Discord IDs or null") from exc

        return cls(
            discord_token=discord_token,
            woolix_api_key=api_key,
            woolix_api_base_url=str(woolix_config.get("base_url") or "https://woolix.net").rstrip("/"),
            discord_guild_id=guild_id,
            log_channel_id=log_channel_id,
            owner_user_ids=_ids("discord.owner_user_ids", discord_config.get("owner_user_ids")),
            allowed_user_ids=_ids("discord.allowed_user_ids", discord_config.get("allowed_user_ids")),
            allowed_user_expirations=_expirations(
                "discord.allowed_user_expirations",
                discord_config.get("allowed_user_expirations"),
            ),
            api_timeout_seconds=_positive_float(
                "woolix.api_timeout_seconds", woolix_config.get("api_timeout_seconds"), 30
            ),
            draft_timeout_seconds=_positive_float(
                "woolix.draft_timeout_seconds", woolix_config.get("draft_timeout_seconds"), 180
            ),
            settlement_timeout_seconds=_positive_float(
                "woolix.settlement_timeout_seconds", woolix_config.get("settlement_timeout_seconds"), 180
            ),
            poll_interval_seconds=_positive_float(
                "woolix.poll_interval_seconds", woolix_config.get("poll_interval_seconds"), 1.5
            ),
            bot_name=str(bot_config.get("name") or "OVIO DD").strip() or "OVIO DD",
            maintenance=bool(bot_config.get("maintenance", False)),
            maintenance_message=str(
                bot_config.get("maintenance_message")
                or "OVIO DD is being updated. Please try again soon."
            ).strip(),
            success_webhook_url=str(bot_config.get("success_webhook_url") or "").strip(),
            database_url=str(database_config.get("url") or "").strip(),
            tracker_base_url=str(
                tracking_config.get("base_url")
                or "https://food-order-tracker.up.railway.app/"
            ).strip(),
            tracker_slug_secret=str(tracking_config.get("slug_secret") or "").strip(),
            config_path=target,
        )

    def save_runtime(self) -> None:
        """Persist admin-editable values while preserving tokens and API keys."""
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        discord_config = raw.setdefault("discord", {})
        bot_config = raw.setdefault("bot", {})
        discord_config["owner_user_ids"] = sorted(self.owner_user_ids)
        discord_config["allowed_user_ids"] = sorted(self.allowed_user_ids)
        discord_config["allowed_user_expirations"] = {
            str(user_id): expires_at
            for user_id, expires_at in sorted(self.allowed_user_expirations.items())
            if user_id in self.allowed_user_ids
        }
        bot_config["maintenance"] = self.maintenance
        bot_config["maintenance_message"] = self.maintenance_message
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.config_path)
