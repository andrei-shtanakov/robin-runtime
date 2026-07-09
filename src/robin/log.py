"""Shared logging setup: one format, and no secrets in the journal.

httpx logs every request URL at INFO — for the Telegram Bot API that URL contains the
bot token (slot 17: secrets must not leak into logs), so third-party HTTP loggers are
capped at WARNING.
"""

from __future__ import annotations

import logging


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    for noisy in ("httpx", "httpcore", "telegram.request"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
