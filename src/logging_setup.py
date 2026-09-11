"""Redacted console and rotating-file logging for OVIO DD."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .error_reporting import redact_text


LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


class RedactingFormatter(logging.Formatter):
    """Apply credential and payment redaction after the record is formatted."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_text(super().format(record))


def configure_logging(log_path: Path) -> None:
    """Configure stdout plus a bounded local file without duplicate handlers."""
    root = logging.getLogger()
    if any(getattr(handler, "ovio_dd_handler", False) for handler in root.handlers):
        return

    formatter = RedactingFormatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.ovio_dd_handler = True  # type: ignore[attr-defined]

    log_path.parent.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(
        log_path,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    rotating.ovio_dd_handler = True  # type: ignore[attr-defined]

    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(rotating)
