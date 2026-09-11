"""Crash-safe per-checkout fee ledger and nightly reminder selection."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


CHECKOUT_FEE_CENTS = 350
NIGHTLY_HOUR = 23
NIGHTLY_TIMEZONE = ZoneInfo("America/Los_Angeles")
TABLE_NAME = "ovio_checkout_fees"


def nightly_cutoff(*, now: datetime | None = None) -> datetime:
    """Return the latest elapsed 11 PM Pacific billing cutoff."""
    current = (now or datetime.now(tz=NIGHTLY_TIMEZONE)).astimezone(NIGHTLY_TIMEZONE)
    cutoff = current.replace(hour=NIGHTLY_HOUR, minute=0, second=0, microsecond=0)
    if current < cutoff:
        cutoff -= timedelta(days=1)
    return cutoff


class BillingStore:
    def __init__(self, path: Path, database_url: str = ""):
        self.path = path
        self.database_url = database_url.strip()
        self.records = self._load()
        if self.database_url:
            self._initialize_database()
            self._migrate_json_records()

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:
            raise RuntimeError("Postgres support requires psycopg[binary]") from exc
        return psycopg.connect(self.database_url, connect_timeout=10)

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
                    job_id TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                    created_at TIMESTAMPTZ NOT NULL,
                    reminded_at TIMESTAMPTZ,
                    paid_at TIMESTAMPTZ
                )
            """)
            connection.execute(f"""
                CREATE INDEX IF NOT EXISTS {TABLE_NAME}_user_unpaid_idx
                ON {TABLE_NAME} (user_id, created_at)
                WHERE paid_at IS NULL
            """)

    @staticmethod
    def _timestamp(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    @staticmethod
    def _record_from_row(row: tuple[Any, ...]) -> dict[str, Any]:
        job_id, user_id, amount_cents, created_at, reminded_at, paid_at = row
        return {
            "job_id": str(job_id),
            "user_id": int(user_id),
            "amount_cents": int(amount_cents),
            "created_at": created_at.timestamp(),
            "reminded_at": reminded_at.timestamp() if reminded_at else None,
            "paid_at": paid_at.timestamp() if paid_at else None,
        }

    def _migrate_json_records(self) -> None:
        if not self.records:
            return
        with self._connect() as connection:
            for record in self.records:
                job_id = str(record.get("job_id") or "").strip()
                try:
                    user_id = int(record.get("user_id") or 0)
                    amount_cents = int(record.get("amount_cents") or CHECKOUT_FEE_CENTS)
                except (TypeError, ValueError):
                    continue
                created_at = self._timestamp(record.get("created_at"))
                if not job_id or user_id <= 0 or created_at is None:
                    continue
                connection.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                        (job_id, user_id, amount_cents, created_at, reminded_at, paid_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                    """,
                    (
                        job_id,
                        user_id,
                        amount_cents,
                        created_at,
                        self._timestamp(record.get("reminded_at")),
                        self._timestamp(record.get("paid_at")),
                    ),
                )

    def _load(self) -> list[dict[str, Any]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [record for record in raw if isinstance(record, dict)] if isinstance(raw, list) else []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        target = self.path.with_suffix(".tmp")
        target.write_text(json.dumps(self.records, indent=2), encoding="utf-8")
        target.replace(self.path)

    def record_checkout(self, user_id: int, job_id: str, *, now: datetime | None = None) -> bool:
        job_id = str(job_id).strip()
        if not job_id:
            return False
        created = now or datetime.now(tz=NIGHTLY_TIMEZONE)
        record = {
            "user_id": int(user_id),
            "job_id": job_id,
            "amount_cents": CHECKOUT_FEE_CENTS,
            "created_at": created.timestamp(),
            "reminded_at": None,
            "paid_at": None,
        }
        if self.database_url:
            with self._connect() as connection:
                result = connection.execute(
                    f"""
                    INSERT INTO {TABLE_NAME} (job_id, user_id, amount_cents, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (job_id) DO NOTHING
                    RETURNING job_id
                    """,
                    (job_id, int(user_id), CHECKOUT_FEE_CENTS, created),
                ).fetchone()
            if result is None:
                return False
            self.records.append(record)
            return True
        if any(str(existing.get("job_id")) == job_id for existing in self.records):
            return False
        self.records.append(record)
        self._save()
        return True

    def balance_summary(self, user_id: int) -> dict[str, Any]:
        """Return this user's unpaid checkout-fee balance."""
        wanted = int(user_id)
        if self.database_url:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT job_id, user_id, amount_cents, created_at, reminded_at, paid_at
                    FROM {TABLE_NAME}
                    WHERE user_id = %s AND paid_at IS NULL
                    ORDER BY created_at
                    """,
                    (wanted,),
                ).fetchall()
            records = [self._record_from_row(row) for row in rows]
        else:
            records = [
                record
                for record in self.records
                if int(record.get("user_id") or 0) == wanted and record.get("paid_at") is None
            ]
        return {
            "user_id": wanted,
            "count": len(records),
            "total_cents": sum(
                int(record.get("amount_cents") or CHECKOUT_FEE_CENTS)
                for record in records
            ),
            "records": records,
        }

    def clear_balance(self, user_id: int, *, now: datetime | None = None) -> dict[str, Any]:
        """Mark every unpaid checkout fee for a user as paid and return what changed."""
        wanted = int(user_id)
        paid = now or datetime.now(tz=NIGHTLY_TIMEZONE)
        if self.database_url:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    UPDATE {TABLE_NAME}
                    SET paid_at = %s
                    WHERE user_id = %s AND paid_at IS NULL
                    RETURNING job_id, amount_cents
                    """,
                    (paid, wanted),
                ).fetchall()
            job_ids = [str(row[0]) for row in rows]
            total_cents = sum(int(row[1]) for row in rows)
        else:
            job_ids = []
            total_cents = 0
            paid_at = paid.timestamp()
            for record in self.records:
                if int(record.get("user_id") or 0) != wanted or record.get("paid_at") is not None:
                    continue
                record["paid_at"] = paid_at
                job_ids.append(str(record.get("job_id") or ""))
                total_cents += int(record.get("amount_cents") or CHECKOUT_FEE_CENTS)
            if job_ids:
                self._save()

        # Keep the in-process legacy cache aligned when Postgres is authoritative.
        if self.database_url and job_ids:
            paid_at = paid.timestamp()
            changed = set(job_ids)
            for record in self.records:
                if str(record.get("job_id") or "") in changed:
                    record["paid_at"] = paid_at

        return {
            "user_id": wanted,
            "count": len(job_ids),
            "total_cents": total_cents,
            "job_ids": job_ids,
        }

    def due_summaries(self, *, now: datetime | None = None) -> list[dict[str, Any]]:
        cutoff = nightly_cutoff(now=now)
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if self.database_url:
            with self._connect() as connection:
                rows = connection.execute(
                    f"""
                    SELECT job_id, user_id, amount_cents, created_at, reminded_at, paid_at
                    FROM {TABLE_NAME}
                    WHERE paid_at IS NULL
                      AND created_at <= %s
                      AND (reminded_at IS NULL OR reminded_at < %s)
                    ORDER BY created_at
                    """,
                    (cutoff, cutoff),
                ).fetchall()
            records = [self._record_from_row(row) for row in rows]
        else:
            records = self.records
        for record in records:
            created_at = self._timestamp(record.get("created_at"))
            reminded_at = self._timestamp(record.get("reminded_at"))
            if (
                record.get("paid_at") is None
                and created_at is not None
                and created_at <= cutoff
                and (reminded_at is None or reminded_at < cutoff)
            ):
                grouped[int(record["user_id"])].append(record)
        return [
            {
                "user_id": user_id,
                "job_ids": [str(record["job_id"]) for record in records],
                "count": len(records),
                "total_cents": sum(int(record.get("amount_cents") or CHECKOUT_FEE_CENTS) for record in records),
            }
            for user_id, records in grouped.items()
        ]

    def mark_reminded(self, job_ids: list[str], *, now: datetime | None = None) -> None:
        wanted = set(job_ids)
        reminded = now or datetime.now(tz=NIGHTLY_TIMEZONE)
        if self.database_url:
            with self._connect() as connection:
                connection.execute(
                    f"""
                    UPDATE {TABLE_NAME}
                    SET reminded_at = %s
                    WHERE job_id = ANY(%s) AND paid_at IS NULL
                    """,
                    (reminded, list(wanted)),
                )
            return
        reminded_at = reminded.timestamp()
        changed = False
        for record in self.records:
            if str(record.get("job_id")) in wanted and record.get("paid_at") is None:
                record["reminded_at"] = reminded_at
                changed = True
        if changed:
            self._save()
