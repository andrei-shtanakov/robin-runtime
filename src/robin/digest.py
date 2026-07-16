"""Ecosystem digest duty (duties.md #2, ROBIN-SPEC M2): compose, post, persist.

Run by systemd timers: `python -m robin.digest daily|weekly`. Window = since the last
persisted marker (fallback: one cadence). Output goes to the team Telegram channel
(escaped per §6.7) and to var/digests/ — Robin's own store, which read_roots() exposes so
"what did I miss?" is answerable from persisted digests. Never writes the KB."""

from __future__ import annotations

import asyncio
import json
import logging
import re
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
    "Write the {kind} ecosystem digest for the team, covering the digest window "
    "({period}). Structure: 1) what was DONE in each repo over the period (collapse "
    "near-duplicate commits; repos with no visible activity get one short collective "
    "line); 2) what remains NOT done against the plan — only if open plan items appear "
    "in the SOURCES, otherwise omit the section; 3) unresolved questions the changes "
    "raise. Be concise."
)

# Digest-specific composition rules: replaces _ANSWER_RULES for this surface only.
# The digest is chat prose for humans, so the `path:line` citation contract is dropped;
# the negative-evidence invariant (incident 2026-07-09) is kept verbatim in spirit.
_DIGEST_RULES = (
    "Compose a team digest using ONLY the SOURCES below — never invent activity. "
    "The digest is read by humans in a chat channel: do NOT include file paths, line "
    "numbers, commit hashes, document names, or any other source citations — plain "
    "prose only. "
    "Write the digest in Russian only. "
    "NEGATIVE EVIDENCE RULE: empty or irrelevant SOURCES are NEVER proof of absence. "
    "Never assert that something does not exist, did not happen, or 'there were no "
    "changes' merely because the SOURCES are silent — say that no activity is visible "
    "to your tools instead. Distinguish 'I found nothing' from 'there is nothing'. "
    "COVERAGE RULE: name only repos that appear in the SOURCES. The '(watched-repos)' "
    "source is the complete list of repos your tools can see — repos outside it were "
    "NOT checked, so never mention them, not even as quiet or unchanged. If the "
    "SOURCES flag the plan list as partial, say the plan picture is incomplete."
)

# Plan grounding for section 2: open (unchecked) checklist items from each mirror's
# plan files. Checkbox syntax is the only machine-detectable "remaining work" marker;
# repos without plan files simply contribute nothing and the section is omitted.
# docs/plans/*.md is deliberately excluded: those checklists are implementation
# micro-steps ("add file X", "run targeted tests"), not team-level remaining work —
# they flooded the count (221 items on 2026-07-16, mostly micro-steps).
_PLAN_GLOBS = ("TODO.md", "ROADMAP.md")
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]\s+\S")


def plan_hits(config: RobinConfig, *, max_hits: int = 30) -> list[Hit]:
    """Open plan items across the mirrors, as prompt hits labeled 'open plan item'.

    Repos are interleaved round-robin so one long TODO cannot crowd the others out
    of the budget (incident 2026-07-16: atp-platform's 34 items silently displaced
    Maestro's 22). Truncation is disclosed via a trailing marker hit, never silent."""
    # Mirrors only — read_roots() also exposes var/digests (Robin's own outputs),
    # which must never masquerade as a repo plan.
    per_repo: list[list[Hit]] = []
    for root in [config.vault_path, *config.repo_paths]:
        items: list[Hit] = []
        for pattern in _PLAN_GLOBS:
            for path in sorted(root.glob(pattern)):
                try:
                    lines = path.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines()
                except OSError:
                    continue
                rel = f"{root.name}/{path.relative_to(root)}"
                for number, line in enumerate(lines, 1):
                    if _UNCHECKED.match(line):
                        items.append(
                            Hit(rel, number, f"open plan item: {line.strip()[:220]}")
                        )
        if items:
            per_repo.append(items)
    total = sum(len(items) for items in per_repo)
    hits: list[Hit] = []
    for rank in range(max((len(items) for items in per_repo), default=0)):
        for items in per_repo:
            if rank < len(items):
                hits.append(items[rank])
        if len(hits) >= max_hits:
            break
    hits = hits[:max_hits]
    if total > len(hits):
        hits.append(
            Hit(
                "(plan-items-truncated)",
                1,
                f"only {len(hits)} of {total} open plan items fit above — the plan "
                "list is PARTIAL, not the full remaining work.",
            )
        )
    return hits


def watched_repos_hit(config: RobinConfig) -> Hit:
    """The complete set of repos the digest tools can see — grounds the 'quiet repos'
    line and stops the model from naming repos it never checked (COVERAGE RULE)."""
    names = ", ".join(root.name for root in [config.vault_path, *config.repo_paths])
    return Hit("(watched-repos)", 1, f"repos visible to my tools this window: {names}")


def _digest_dir(config: RobinConfig) -> Path:
    return config.var_dir / "digests"


def _marker(config: RobinConfig, kind: str) -> Path:
    return _digest_dir(config) / f"last-{kind}.txt"


def latest(config: RobinConfig, limit: int = 2, max_chars: int = 1200) -> list[str]:
    """Newest persisted digests, flattened to one line each and truncated — the "recent
    digests" half of §6.2 ambient context. Filenames are date-prefixed (persist()), so
    name order is time order."""
    directory = _digest_dir(config)
    if not directory.is_dir():
        return []
    excerpts: list[str] = []
    for path in sorted(directory.glob("*.md"), reverse=True)[:limit]:
        # one prompt bullet per digest — same one-line rule as channel messages
        content = re.sub(
            r"\s+", " ", path.read_text(encoding="utf-8", errors="ignore")
        ).strip()
        excerpts.append(f"{path.name}: {content[:max_chars]}")
    return excerpts


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
    sources = [
        watched_repos_hit(config),
        *collect_changes(config, period, max_hits=60),
        *plan_hits(config),
    ]
    question = _DIGEST_QUESTION.format(kind=kind, period=period.label)
    text, cost = _compose_answer(question, sources, config, rules=_DIGEST_RULES)
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
    kind_ru = {"daily": "дневной дайджест", "weekly": "недельный дайджест"}.get(
        kind, f"{kind} digest"
    )
    html = f"<b>Robin — {fmt.escape_html(kind_ru)}</b>\n\n{fmt.escape_html(text)}"
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
