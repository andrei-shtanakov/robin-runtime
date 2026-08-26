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
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import fmt
from .agent import _compose_answer  # same single LLM call site
from .changes import Period, collect_changes
from .config import RobinConfig, load_config
from .freshness import freshness_hit
from .kb import Hit
from .log import setup_logging
from .plan_state import coverage_hit, delta_hit, fields_hit, open_items, unblocked_hit
from .plan_state import record as record_plan_state

logger = logging.getLogger("robin.digest")

CADENCE_HOURS = {"daily": 24, "weekly": 24 * 7}
# Commit hits per digest, sized to the window: the mirrors produced 83 commits in the
# 07-13→07-20 week, so one flat budget left the weekly digest permanently truncated.
CHANGE_HITS = {"daily": 60, "weekly": 150}

_DIGEST_QUESTION = (
    "Write the {kind} ecosystem digest for the team, covering the digest window "
    "({period}). Structure: 1) what was DONE in each repo over the period (collapse "
    "near-duplicate commits; repos with no visible activity get one short collective "
    "line); 2) how the plan MOVED since the previous digest — how many items opened "
    "and closed and which ones, plus anything open unusually long — from the "
    "'(plan-delta)' source, omitted if that source is absent; 2b) items whose blocker "
    "was DELIVERED and that now await action — from the '(plan-unblocked)' source, "
    "omitted if that source is absent; 3) what remains NOT done "
    "against the plan — only if open plan items appear in the SOURCES, otherwise omit "
    "the section; 4) unresolved questions the changes raise. Be concise."
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
    "SOURCES flag the plan list or the change list as partial, say so — and name the "
    "repos the marker names as only partially covered, rather than warning in general. "
    "If the plan-delta source says there is no previous snapshot, say the comparison "
    "baseline is missing — never report that as 'nothing moved'. If the plan-coverage "
    "source names repos without a plan file, say which repos the remaining-work "
    "picture does NOT cover, once, in plain language. If the plan-fields source "
    "reports items nobody owns, give the count as a single plain sentence — it is a "
    "state of the plan, not a scolding, and never a per-item list. "
    "If the '(plan-unblocked)' source is present, give it its own short section: "
    "these are waits whose blocker was DELIVERED and that still await action — "
    "present them as a wake-up call, not as a list of everything that got "
    "unblocked. If that source says the baseline is missing or UNKNOWN, report "
    "the comparison as unavailable — never as 'nothing was unblocked'. Keep its "
    "coverage caveat: issue-form blockers are only partially covered. "
    "If the '(arch-evidence-freshness)' source is present, give it one plain "
    "sentence: SILENT, UNKNOWN or failed states must be reported as 'freshness "
    "unknown / the watch did not confirm clean' — never softened into reassurance; "
    "a clean state needs at most a brief mention. "
    "AUDIENCE RULE: the digest is read by a mixed team including non-engineers. "
    "Summarize remaining plan work thematically, in plain language grounded in the "
    "SOURCES — a few sentences per repo, not an item-by-item list. Internal shorthand "
    "codes (P1, R-06b, M3-obs and the like) are not self-explanatory: never present "
    "a code as the name of a work item — either drop the code or pair it with the "
    "plain-language description the source gives. If a source line is only a code "
    "with no explanation, fold it into the theme rather than quoting it verbatim."
)

# Plan grounding for section 2: open (unchecked) checklist items from each mirror's
# plan files. Checkbox syntax is the only machine-detectable "remaining work" marker;
# repos without plan files simply contribute nothing and the section is omitted.
# docs/plans/*.md is deliberately excluded: those checklists are implementation
# micro-steps ("add file X", "run targeted tests"), not team-level remaining work —
# they flooded the count (221 items on 2026-07-16, mostly micro-steps). The globs and
# the scanner live in plan_state, which also keeps the between-run snapshot — one
# scanner, so the budgeted list and the delta counters cannot drift apart.
# Plan files move on a weekly scale, so re-sending them every day produced two
# consecutive daily digests whose prose was near-identical (report review 2026-07-26).
# Section 2 is dropped from the prompt when no plan hits are present, so restricting
# the full list to the weekly digest makes the daily one a pure "what moved" delta —
# the daily still carries the movement counters (plan_state.delta_hit).
PLAN_SECTION_KINDS = ("weekly",)
_HIT_CHARS = 260  # per plan hit, fields included — not text plus fields
_FIELDS_CHARS = 120  # of that budget, the most the glossed fields may take


def plan_hits(config: RobinConfig, *, max_hits: int = 80) -> list[Hit]:
    """Open plan items across the mirrors, as prompt hits labeled 'open plan item'.

    Repos are interleaved round-robin so one long TODO cannot crowd the others out
    of the budget (incident 2026-07-16: atp-platform's 34 items silently displaced
    Maestro's 22). Truncation is disclosed via a trailing marker hit that names the
    repos left partial — a bare "30 of 62" tells the reader the picture is incomplete
    but not where the hole is (report review 2026-07-26)."""
    # Mirrors only — read_roots() also exposes var/digests (Robin's own outputs),
    # which must never masquerade as a repo plan. plan_state.open_items() is the one
    # scanner: the delta counters and this budgeted list must not drift apart.
    grouped: dict[str, list[Hit]] = {}
    for item in open_items(config):
        # Carry the enclosing markdown heading into each hit: checkbox lines alone are
        # dev shorthand ("P4 + prefill"), and the section title is the plain-language
        # context the AUDIENCE RULE needs to gloss them.
        label = (
            f"open plan item ({item.heading}): " if item.heading else "open plan item: "
        )
        # Fields are glossed, never passed through as tag syntax: the digest is read by
        # a mixed team, and `@blocked_by:Maestro#R-03` is not a sentence (AUDIENCE RULE).
        fields = [
            f"{name}: {value}"
            for name, value in (
                ("owner", item.owner),
                ("blocked by", item.blocked_by),
                ("trigger", item.trigger),
            )
            if value
        ]
        # The fields come out of the item's own budget, not on top of it: every hit
        # that grows costs the prompt a source it could have carried. Prose is the
        # compressible part — the fields are short, structured, and what the blocking
        # graph is made of, so they survive whole and the text yields.
        suffix = f" — {'; '.join(fields)}"[:_FIELDS_CHARS] if fields else ""
        body = (label + item.text)[: _HIT_CHARS - len(suffix)]
        grouped.setdefault(item.path.split("/")[0], []).append(
            Hit(item.path, item.line, body + suffix)
        )
    per_repo = list(grouped.values())
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
        shown = Counter(hit.path.split("/")[0] for hit in hits)
        partial = [
            items[0].path.split("/")[0]
            for items in per_repo
            if shown[items[0].path.split("/")[0]] < len(items)
        ]
        hits.append(
            Hit(
                "(plan-items-truncated)",
                1,
                f"only {len(hits)} of {total} open plan items fit above — the plan "
                "list is PARTIAL, not the full remaining work. Repos shown only in "
                f"part: {', '.join(partial)}.",
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
    config: RobinConfig,
    kind: str,
    *,
    now: datetime | None = None,
    period: Period | None = None,
) -> tuple[str, list[Hit], float | None]:
    """Compose the digest text via the standard grounded pipeline.

    `period` lets run() own the window: the epic-axis shadow must see the very
    same Period the digest was composed over (spec §3.1), and window() cannot
    be re-called after persist() moves the marker.
    """
    if period is None:
        period = window(config, kind, now=now)
    sources = [
        watched_repos_hit(config),
        *collect_changes(config, period, max_hits=CHANGE_HITS[kind]),
    ]
    if movement := delta_hit(config, kind, now=now):
        sources.append(movement)
    # The daily human read of a delivered blocker (issue #47): the red scheduled
    # run is machine evidence, the digest is where a person actually sees it.
    if unblocked := unblocked_hit(config, kind):
        sources.append(unblocked)
    if gap := coverage_hit(config):
        sources.append(gap)
    if unlabelled := fields_hit(config, kind):
        sources.append(unlabelled)
    # Second clock for steward's cron watch (issue #42): a scheduler cannot report
    # its own silence, so every digest carries the last run's verdict — or an
    # explicit "could not read", never a silent drop.
    if freshness := freshness_hit(config, now=now):
        sources.append(freshness)
    if kind in PLAN_SECTION_KINDS:
        sources += plan_hits(config, max_hits=config.plan_items_max)
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


def run(kind: str, *, now: datetime | None = None) -> None:
    config = load_config()
    # The run's single clock: resolved exactly once, then only passed along. It
    # pins the window top (until=now instead of a floating "now of each git
    # log"), stamps generated_at, and is what the determinism test injects.
    zone = ZoneInfo(config.tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    base = window(config, kind, now=now)
    period = Period(since=base.since, until=now, label=base.label)
    text, sources, cost = compose(config, kind, period=period)
    path = persist(config, kind, text, now=now)
    logger.info("digest persisted: %s (%d sources, cost=%s)", path, len(sources), cost)
    asyncio.run(post(config, text, kind))
    # Baseline advances only once the digest has reached the team — persisting is not
    # delivery, and a run that died in post() must re-report its movement next time.
    # (A plan file edited between compose and here lands in the next window's delta.)
    record_plan_state(config, kind)
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
