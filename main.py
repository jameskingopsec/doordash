"""Launch the Woolix-backed OVIO DD Discord bot."""

from __future__ import annotations

import asyncio

from src.bot import run_bot
from src.config import DATA_DIR
from src.logging_setup import configure_logging


if __name__ == "__main__":
    configure_logging(DATA_DIR / "ovio-dd.log")
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
