"""Ecosystem digest duty (duties.md #2, ROBIN-SPEC M2): compose, post, persist.

Run by systemd timers: `python -m robin.digest daily|weekly`. Window = since the last
persisted marker (fallback: one cadence). Output goes to the team Telegram channel
(escaped per §6.7) and to var/digests/ — Robin's own store, which read_roots() exposes so
"what did I miss?" is answerable from persisted digests. Never writes the KB."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import fmt
from .agent import _compose_answer  # same single LLM call site
from .changes import Period, collect_changes
from .config import RobinConfig, load_config
from .kb import Hit
from .log import setup_logging

logger = logging.getLogger("robin.digest")

CADENCE_HOURS = {"daily": 24, "weekly": 24 * 7}

_DIGEST_QUESTION = (
    "Write the {kind} ecosystem digest for the team: what moved in each repo (collapse "
    "near-duplicate commits), which repos did NOT move, and any open questions the changes "
    "raise. Be concise; every claim cites its source."
)


def _digest_dir(config: RobinConfig) -> Path:
    return config.var_dir / "digests"


def _marker(config: RobinConfig, kind: str) -> Path:
    return _digest_dir(config) / f"last-{kind}.txt"


def window(config: RobinConfig, kind: str, *, now: datetime | None = None) -> Period:
    """Since the last successful digest of this kind; fallback: one cadence back."""
    zone = ZoneInfo(config.tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    fallback = now - timedelta(hours=CADENCE_HOURS[kind])
    marker = _marker(config, kind)
    since = fallback
    if marker.is_file():
        try:
            since = datetime.fromtimestamp(int(marker.read_text().strip()), zone)
        except ValueError:
            logger.warning("unreadable marker %s; using cadence fallback", marker)
    return Period(since=min(since, now), until=None, label=f"{kind} digest window")


def compose(
    config: RobinConfig, kind: str, *, now: datetime | None = None
) -> tuple[str, list[Hit], float | None]:
    """Compose the digest text via the standard grounded pipeline."""
    period = window(config, kind, now=now)
    sources = collect_changes(config, period, max_hits=60)
    text, cost = _compose_answer(_DIGEST_QUESTION.format(kind=kind), sources, config)
    return text, sources, cost


def persist(
    config: RobinConfig, kind: str, text: str, *, now: datetime | None = None
) -> Path:
    """var/digests/YYYY-MM-DD-<kind>.md + refresh the marker (liveness reads its mtime)."""
    zone = ZoneInfo(config.tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    directory = _digest_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now:%Y-%m-%d}-{kind}.md"
    content = f"# {kind.capitalize()} digest — {now:%Y-%m-%d %H:%M %Z}\n\n{text}\n"
    path.write_text(content)
    if path.read_text() != content:  # §6.4 read-back verification, not optional
        raise RuntimeError(f"read-back verification failed for {path}")
    _marker(config, kind).write_text(str(int(now.timestamp())))
    return path


async def post(config: RobinConfig, text: str, kind: str) -> None:
    """Post to the team channel. §6.7: escape first; rejected send = logged failure record."""
    if not (config.telegram_token and config.telegram_channel):
        logger.warning("no telegram channel configured; digest persisted only")
        return
    from telegram import Bot
    from telegram.error import BadRequest

    bot = Bot(config.telegram_token)
    html = f"<b>Robin — {kind} digest</b>\n\n{fmt.escape_html(text)}"
    for part in fmt.chunk(html):
        try:
            await bot.send_message(config.telegram_channel, part, parse_mode="HTML")
        except BadRequest as exc:
            _log_failure(config, kind, f"formatting-rejected send: {exc}")
            await bot.send_message(config.telegram_channel, part)


def _log_failure(config: RobinConfig, kind: str, error: str) -> None:
    config.var_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "surface": "digest",
        "kind": kind,
        "ok": False,
        "error": error,
    }
    with (config.var_dir / "interactions.jsonl").open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    logger.error("digest %s: %s", kind, error)


def run(kind: str) -> None:
    config = load_config()
    text, sources, cost = compose(config, kind)
    path = persist(config, kind, text)
    logger.info("digest persisted: %s (%d sources, cost=%s)", path, len(sources), cost)
    asyncio.run(post(config, text, kind))
    # digest runs are interactions too (§7 cost observability)
    config.var_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "surface": "digest",
        "kind": kind,
        "n_sources": len(sources),
        "cost_usd": cost,
        "ok": True,
    }
    with (config.var_dir / "interactions.jsonl").open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> None:
    setup_logging()
    kind = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if kind not in CADENCE_HOURS:
        raise SystemExit(f"usage: python -m robin.digest {'|'.join(CADENCE_HOURS)}")
    run(kind)


if __name__ == "__main__":
    main()
