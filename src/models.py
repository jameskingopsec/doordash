"""Order state and input parsing that stay independent from Discord."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import json
import re
from typing import Any


class FlowState(StrEnum):
    DETAILS = "details"
    PAYMENT = "payment"
    CONFIRMING_DRAFT = "confirming_draft"
    BUILDING_DRAFT = "building_draft"
    DRAFT_READY = "draft_ready"
    PLACING = "placing"
    PLACED = "placed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_JOB_STATUSES = frozenset({"placed", "cancelled", "expired", "failed"})
SETTLED_PAYMENT_STATUSES = frozenset({"succeeded", "declined", "failed", "unverified"})


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def parse_expiry(value: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\D+", value.strip()) if p]
    if len(parts) != 2:
        raise ValueError("Expiry must use MM/YY")
    month = parts[0].zfill(2)
    year = parts[1]
    if len(year) == 4:
        year = year[-2:]
    if not (month.isdigit() and 1 <= int(month) <= 12 and len(year) == 2 and year.isdigit()):
        raise ValueError("Expiry must use a valid MM/YY")
    return month, year


def parse_card(number: str, expiry: str, cvv: str, zip_code: str) -> dict[str, str]:
    clean_number = digits(number)
    clean_cvv = digits(cvv)
    clean_zip = re.sub(r"[^0-9A-Za-z -]", "", zip_code).strip()
    month, year = parse_expiry(expiry)
    if not 13 <= len(clean_number) <= 19:
        raise ValueError("Card number must contain 13–19 digits")
    if len(clean_cvv) not in (3, 4):
        raise ValueError("CVV must contain 3 or 4 digits")
    if not clean_zip:
        raise ValueError("Billing ZIP is required")
    return {
        "number": clean_number,
        "exp_month": month,
        "exp_year": year,
        "cvv": clean_cvv,
        "zip": clean_zip,
    }


def parse_card_profile(value: str) -> dict[str, str]:
    """Parse the API card object from one Discord modal input."""
    raw = value.strip()
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("Card JSON is invalid") from exc
        if not isinstance(data, dict):
            raise ValueError("Card JSON must be an object")
        expiry = str(data.get("expiry") or f"{data.get('exp_month', '')}/{data.get('exp_year', '')}")
        return parse_card(
            str(data.get("number") or ""),
            expiry,
            str(data.get("cvv") or ""),
            str(data.get("zip") or data.get("zip_code") or ""),
        )

    separator = "|" if "|" in raw else ","
    parts = [part.strip() for part in raw.split(separator)]
    if len(parts) != 4:
        raise ValueError("Card must use NUMBER | MM/YY | CVV | ZIP")
    return parse_card(*parts)


def money(cents: Any) -> str:
    try:
        amount = int(cents or 0)
    except (TypeError, ValueError):
        amount = 0
    return f"${amount / 100:,.2f}"


def mask_card(last4: str) -> str:
    return f"•••• {last4[-4:]}" if last4 else "Not added"


@dataclass(slots=True)
class OrderSession:
    user_id: int
    fulfillment: str = "delivery"
    group_cart: str = ""
    address: str = ""
    unit: str = ""
    name: str = ""
    delivery_note: str = ""
    promo: str = ""
    tip_cents: int = 0
    store_name: str = ""
    card_last4: str = ""
    job_id: str = ""
    state: FlowState = FlowState.DETAILS
    job: dict[str, Any] = field(default_factory=dict)
    success_announced: bool = False
    integrity_rebuild_attempted: bool = False
    card: dict[str, str] | None = field(default=None, repr=False)

    def create_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"group_cart": self.group_cart}
        if self.address:
            payload["address"] = self.address
        if self.fulfillment and self.fulfillment != "delivery":
            payload["fulfillment"] = self.fulfillment
        if self.name:
            payload["name"] = self.name
        if self.tip_cents:
            payload["tip"] = self.tip_cents
        if self.unit:
            payload["unit"] = self.unit
        if self.delivery_note:
            payload["delivery_note"] = self.delivery_note
        if self.promo:
            payload["promo"] = self.promo
        if self.card:
            payload["card"] = dict(self.card)
        return payload

    def safe_dict(self) -> dict[str, Any]:
        """Serializable state. Card secrets are deliberately never persisted."""
        data = asdict(self)
        data.pop("card", None)
        data["state"] = self.state.value
        return data

    @classmethod
    def from_safe_dict(cls, data: dict[str, Any]) -> "OrderSession":
        allowed = {field.name for field in cls.__dataclass_fields__.values()} - {"card"}
        values = {key: value for key, value in data.items() if key in allowed}
        try:
            values["state"] = FlowState(values.get("state", FlowState.DETAILS))
        except ValueError:
            values["state"] = FlowState.DETAILS
        return cls(**values)
