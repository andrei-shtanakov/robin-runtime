"""§7 liveness: alert the maintainer when the newest digest is older than cadence + grace.

Run hourly by a systemd timer: `python -m robin.liveness`. A silently-dead digest duty is
the spec's canonical failure mode ("always-on" on a machine that sleeps)."""

from __future__ import annotations

import asyncio
import logging
import sys
import time

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
    text = (
        "⚠️ Robin liveness: digest(s) overdue — "
        + ", ".join(kinds)
        + ". Check the robin-digest timers on the VPS."
    )
    if not (config.telegram_token and config.maintainer_chat):
        logger.error("%s (no maintainer chat configured — log-only alert)", text)
        return
    from telegram import Bot

    await Bot(config.telegram_token).send_message(config.maintainer_chat, text)


def main() -> None:
    setup_logging()
    config = load_config()
    kinds = stale_kinds(config)
    if not kinds:
        logger.info("liveness ok")
        return
    asyncio.run(alert(config, kinds))
    sys.exit(1)  # visible to systemd as a failed unit too


if __name__ == "__main__":
    main()
