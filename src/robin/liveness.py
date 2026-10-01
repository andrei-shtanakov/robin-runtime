"""§7 liveness: alert the maintainer when the newest digest is older than cadence + grace.

Run hourly by a systemd timer: `python -m robin.liveness`. A silently-dead digest duty is
the spec's canonical failure mode ("always-on" on a machine that sleeps). The same run
checks external checks by their receipts (`external_checks`, issue #71)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime, timezone

from . import external_checks
from .config import RobinConfig, load_config
from .digest import CADENCE_HOURS, _marker
from .log import setup_logging

logger = logging.getLogger("robin.liveness")


def stale_kinds(config: RobinConfig, *, now: float | None = None) -> list[str]:
    """Digest kinds whose last run is older than cadence + grace (or never ran)."""
    now = now if now is not None else time.time()
    stale: list[str] = []
    for kind, cadence_h in CADENCE_HOURS.items():
        limit = (cadence_h + config.digest_grace_hours) * 3600
        marker = _marker(config, kind)
        try:
            last = int(marker.read_text().strip())
        except (OSError, ValueError):
            stale.append(kind)
            continue
        if now - last > limit:
            stale.append(kind)
    return stale


async def alert(config: RobinConfig, kinds: list[str]) -> None:
    await notify(
        config,
        "⚠️ Robin liveness: digest(s) overdue — "
        + ", ".join(kinds)
        + ". Check the robin-digest timers on the VPS.",
    )


async def notify(config: RobinConfig, text: str) -> bool:
    """Send `text` to the maintainer DM; False when it was only logged (no chat)."""
    if not (config.telegram_token and config.maintainer_chat):
        logger.error("%s (no maintainer chat configured — log-only alert)", text)
        return False
    from telegram import Bot

    await Bot(config.telegram_token).send_message(config.maintainer_chat, text)
    return True


def main() -> None:
    setup_logging()
    config = load_config()
    kinds = stale_kinds(config)
    if kinds:
        asyncio.run(alert(config, kinds))
    else:
        logger.info("liveness ok")
    try:  # never let the external reader mask the digest check above
        delivered = external_checks.run(
            config,
            lambda text: asyncio.run(notify(config, text)),
            datetime.now(timezone.utc),
        )
    except Exception:
        logger.exception("external checks crashed")
        delivered = False
    if kinds or not delivered:
        sys.exit(1)  # visible to systemd as a failed unit too


if __name__ == "__main__":
    main()
