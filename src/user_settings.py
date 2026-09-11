"""Encrypted per-user settings backed by the bot's Postgres database."""

from __future__ import annotations

import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


TABLE_NAME = "ovio_user_settings"
SETTINGS_AAD_PREFIX = b"ovio-dd-user-settings-v1"


def mask_secret(value: str | None) -> str:
    """Return a stable, non-sensitive representation for dashboard display."""
    secret = str(value or "").strip()
    if not secret:
        return "Not connected"
    if len(secret) <= 8:
        return "•" * len(secret)
    return f"{secret[:4]}{'•' * 6}{secret[-4:]}"


class UserSettingsStore:
    """Store provider credentials encrypted at rest.

    Railway uses Postgres. The encrypted in-memory fallback keeps local/test runs
    usable when no database URL is configured without writing credentials to disk.
    """

    def __init__(self, database_url: str, encryption_secret: str):
        self.database_url = str(database_url or "").strip()
        secret = str(encryption_secret or "").strip()
        if not secret:
            raise RuntimeError("A settings encryption secret is required")
        self._key = hashlib.sha256(
            SETTINGS_AAD_PREFIX + b"\0" + secret.encode("utf-8")
        ).digest()
        self._memory: dict[int, tuple[bytes, bytes]] = {}
        if self.database_url:
            self._initialize_database()

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
                    user_id BIGINT PRIMARY KEY,
                    getatext_nonce BYTEA,
                    getatext_ciphertext BYTEA,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

    @staticmethod
    def _aad(user_id: int) -> bytes:
        return SETTINGS_AAD_PREFIX + b":" + str(int(user_id)).encode("ascii") + b":getatext"

    def _encrypt(self, user_id: int, value: str) -> tuple[bytes, bytes]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(
            nonce,
            value.encode("utf-8"),
            self._aad(user_id),
        )
        return nonce, ciphertext

    def _decrypt(self, user_id: int, nonce: bytes, ciphertext: bytes) -> str:
        cleartext = AESGCM(self._key).decrypt(
            bytes(nonce),
            bytes(ciphertext),
            self._aad(user_id),
        )
        return cleartext.decode("utf-8")

    def get_getatext_key(self, user_id: int) -> str | None:
        wanted = int(user_id)
        if self.database_url:
            with self._connect() as connection:
                row = connection.execute(
                    f"""
                    SELECT getatext_nonce, getatext_ciphertext
                    FROM {TABLE_NAME}
                    WHERE user_id = %s
                    """,
                    (wanted,),
                ).fetchone()
            if not row or row[0] is None or row[1] is None:
                return None
            value = self._decrypt(wanted, row[0], row[1]).strip()
            return value or None

        encrypted = self._memory.get(wanted)
        if not encrypted:
            return None
        value = self._decrypt(wanted, *encrypted).strip()
        return value or None

    def set_getatext_key(self, user_id: int, value: str) -> None:
        wanted = int(user_id)
        key = str(value or "").strip()
        if not 8 <= len(key) <= 128:
            raise ValueError("Enter a valid GetAText key")
        nonce, ciphertext = self._encrypt(wanted, key)
        if self.database_url:
            with self._connect() as connection:
                connection.execute(
                    f"""
                    INSERT INTO {TABLE_NAME}
                        (user_id, getatext_nonce, getatext_ciphertext, updated_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        getatext_nonce = EXCLUDED.getatext_nonce,
                        getatext_ciphertext = EXCLUDED.getatext_ciphertext,
                        updated_at = NOW()
                    """,
                    (wanted, nonce, ciphertext),
                )
            return
        self._memory[wanted] = (nonce, ciphertext)

    def clear_getatext_key(self, user_id: int) -> bool:
        wanted = int(user_id)
        if self.database_url:
            with self._connect() as connection:
                row = connection.execute(
                    f"DELETE FROM {TABLE_NAME} WHERE user_id = %s RETURNING user_id",
                    (wanted,),
                ).fetchone()
            return row is not None
        return self._memory.pop(wanted, None) is not None
