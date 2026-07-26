"""Plan movement between digest runs — the delta half of the ecosystem digest.

Checkbox scraping answers "what is open now"; on its own it made two consecutive
daily digests read as the same text (report review 2026-07-26). A snapshot of the
open items, persisted per cadence in Robin's own store, answers the question the
team actually asks: what *moved*.

One snapshot per digest kind (`var/plan-state-<kind>.json`, alongside the existing
`last-<kind>.txt` markers), so a daily run never consumes the weekly baseline.
Items are keyed by file + normalized text rather than by line, so edits elsewhere in
the file do not manufacture a close/open pair; a reworded item does read as one
closed and one opened, which is the honest reading of a checkbox list.

Blocked/unblocked and trigger-reached counters need `owner` / `blocked_by` /
`trigger` fields the plan files do not carry yet; they slot in here once they do.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .config import RobinConfig
from .kb import Hit

logger = logging.getLogger("robin.plan_state")

# Same sources as the digest's plan section: team-level plan files only, never
# docs/plans/*.md implementation micro-steps.
PLAN_GLOBS = ("TODO.md", "ROADMAP.md")
_UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]\s+(\S.*)$")
_HEADING = re.compile(r"^#{1,6}\s+(.+)")

STALE_AFTER_DAYS = 30  # an item open this long is a decision, not a task
_MAX_NAMED = 5  # named examples per counter; the counts carry the rest
_STATE_VERSION = 1


@dataclass(frozen=True)
class PlanItem:
    """One open checklist item, identified independently of its line number."""

    key: str
    repo: str
    path: str  # "<repo>/TODO.md"
    line: int
    heading: str
    text: str


def _key(path: str, text: str) -> str:
    return f"{path}::{' '.join(text.lower().split())}"


def open_items(config: RobinConfig) -> list[PlanItem]:
    """Every open plan item across the mirrors, unbudgeted, in repo-major order.

    The digest's prompt budget is applied downstream (digest.plan_hits); state and
    counters must see the complete set or the delta would report budget artefacts
    as movement."""
    items: list[PlanItem] = []
    for root in [config.vault_path, *config.repo_paths]:
        for pattern in PLAN_GLOBS:
            for path in sorted(root.glob(pattern)):
                try:
                    lines = path.read_text(
                        encoding="utf-8", errors="ignore"
                    ).splitlines()
                except OSError:
                    continue
                rel = f"{root.name}/{path.relative_to(root)}"
                heading = ""
                for number, line in enumerate(lines, 1):
                    if match := _HEADING.match(line):
                        heading = match.group(1).strip().strip("*").strip()[:60]
                        continue
                    if match := _UNCHECKED.match(line):
                        # Checkbox syntax is a marker, not content — it reads as noise
                        # once the item is quoted inside a sentence about movement.
                        text = match.group(1).strip()
                        items.append(
                            PlanItem(
                                key=_key(rel, text),
                                repo=root.name,
                                path=rel,
                                line=number,
                                heading=heading,
                                text=text,
                            )
                        )
    return items


def _state_path(config: RobinConfig, kind: str) -> Path:
    return config.var_dir / f"plan-state-{kind}.json"


def load_state(config: RobinConfig, kind: str) -> dict[str, dict] | None:
    """The previous snapshot, or None when there is no usable one — the caller must
    say "no baseline", never "nothing moved" (negative-evidence rule)."""
    path = _state_path(config, kind)
    if not path.is_file():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return dict(state["items"])
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("unreadable plan snapshot %s; treating as first run", path)
        return None


def record(config: RobinConfig, kind: str, *, now: datetime | None = None) -> Path:
    """Persist the current open items as this cadence's baseline.

    `first_seen` survives re-sighting, so item age is measured from the first digest
    that saw it rather than from the last run."""
    stamp = int(now.timestamp()) if now else int(time.time())
    previous = load_state(config, kind) or {}
    items = {
        item.key: {
            "repo": item.repo,
            "path": item.path,
            "text": item.text,
            "first_seen": previous.get(item.key, {}).get("first_seen", stamp),
            "last_seen": stamp,
        }
        for item in open_items(config)
    }
    config.var_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(config, kind)
    content = json.dumps(
        {"version": _STATE_VERSION, "updated": stamp, "items": items},
        ensure_ascii=False,
        indent=1,
    )
    path.write_text(content, encoding="utf-8")
    if path.read_text(encoding="utf-8") != content:  # §6.4 read-back verification
        raise RuntimeError(f"read-back verification failed for {path}")
    return path


def _summarize(texts: list[str]) -> str:
    shown = "; ".join(text[:120] for text in texts[:_MAX_NAMED])
    if len(texts) > _MAX_NAMED:
        shown += f"; … and {len(texts) - _MAX_NAMED} more"
    return shown


def delta_hit(
    config: RobinConfig, kind: str, *, now: datetime | None = None
) -> Hit | None:
    """What moved in the plan files since this cadence's previous digest.

    Returned as a source hit so the answer layer grounds the movement line the same
    way it grounds everything else. None when there are no plan files at all."""
    stamp = int(now.timestamp()) if now else int(time.time())
    items = open_items(config)
    previous = load_state(config, kind)
    if not items and previous is None:
        return None
    total = f"{len(items)} open plan item(s) across the mirrors now"
    if previous is None:
        return Hit(
            "(plan-delta)",
            1,
            f"{total}, but no previous snapshot for the {kind} cadence to compare "
            "against — this is a first baseline, NOT evidence that nothing moved.",
        )
    current = {item.key: item for item in items}
    opened = [item.text for key, item in current.items() if key not in previous]
    closed = [entry["text"] for key, entry in previous.items() if key not in current]
    parts = [total]
    if opened or closed:
        if opened:
            parts.append(
                f"{len(opened)} newly opened since the previous {kind} "
                f"digest: {_summarize(opened)}"
            )
        if closed:
            parts.append(
                f"{len(closed)} closed or removed since then: {_summarize(closed)}"
            )
    else:
        parts.append(f"no plan items opened or closed since the previous {kind} digest")
    stale = sorted(
        (
            (stamp - previous[key]["first_seen"]) // 86400,
            current[key].text,
        )
        for key in current
        if key in previous
        and (stamp - previous[key]["first_seen"]) >= STALE_AFTER_DAYS * 86400
    )
    if stale:
        oldest = [f"{text} (open for {days} days)" for days, text in reversed(stale)]
        parts.append(
            f"{len(stale)} item(s) open longer than "
            f"{STALE_AFTER_DAYS} days: {_summarize(oldest)}"
        )
    return Hit("(plan-delta)", 1, ". ".join(parts) + ".")
