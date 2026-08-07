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

Operational fields are parsed by the shared plan-fields package.  Robin projects
its typed-owner and blocker diagnostics into an explanatory digest; it does not
define a second owner grammar or graph checker.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from plan_fields import (
    ManifestIndex,
    RepoInput,
    check_fleet,
    check_legacy_fleet,
    parse_fleet,
    parse_owner,
    scrape_items,
)

from .config import RobinConfig
from .kb import Hit

logger = logging.getLogger("robin.plan_state")

# Same sources as the digest's plan section: team-level plan files only, never
# docs/plans/*.md implementation micro-steps.
PLAN_GLOBS = ("TODO.md", "ROADMAP.md")

STALE_AFTER_DAYS = 30  # an item open this long is a decision, not a task
_MAX_NAMED = 5  # named examples per counter; the counts carry the rest
_STATE_VERSION = 1

_OWNERSHIP = (
    "human-owned",
    "repo-owned",
    "TBD",
    "missing",
    "invalid-owner",
    "unknown-repo-owner",
)
_MOVEMENT = (
    "actionable",
    "waiting-by-trigger",
    "waiting-by-blocker",
    "stale-condition",
    "malformed-condition",
)
_MALFORMED_CONDITION_CODES = {
    "PF-ID-DANGLING",
    "PF-BLOCKER-DANGLING",
    "PF-BLOCKER-UNRESOLVABLE",
    "PF-BLOCKER-NO-TODO",
    "PF-BLOCKER-REPO-UNKNOWN",
    "PF-LEGACY-AMBIGUOUS",
}

# The syntactic scan — "what is a checkbox item", including the inline tags
# `@owner` / `@blocked_by` / `@trigger` — is the shared plan-fields package
# (ADR-ECO-005 PF-7). Robin keeps only its own snapshot identity (`_key` below)
# and reads the three operational fields it cares about off the scraped tags.


@dataclass(frozen=True)
class PlanItem:
    """One open checklist item, identified independently of its line number.

    `text` is the prose with the tags removed and the fields lifted out: the tags are
    metadata about the item, not part of what the item says."""

    key: str
    repo: str
    path: str  # "<repo>/TODO.md"
    line: int
    heading: str
    text: str
    owner: str | None = None
    blocked_by: str | None = None
    trigger: str | None = None
    # Field tags found on the item's continuation lines — written by the author,
    # invisible to the line-based parser, so none of the fields above carry them.
    hidden_tags: tuple[str, ...] = ()


def _section(section: str | None) -> str:
    """Robin's heading budget on the shared scraper's section (a `*`-bold heading
    reads as its text; long headings are trimmed to keep the delta line short)."""
    return (section or "").strip("*").strip()[:60]


def _key(path: str, text: str) -> str:
    """Identity of an item: its file and its prose, with tags already stripped
    (by plan-fields `scrape_items` -> `display_text`). Labelling an item, or
    changing who owns it, must not read as the old item closing and a new one
    opening (handoff note 2026-07-26 §4)."""
    return f"{path}::{' '.join(text.lower().split())}"


# The field keys a continuation line can hide. Restricted to the keys consumers
# act on, so an incidental `@word:value` in wrapped prose does not flag the item.
_FIELD_KEYS = frozenset({"owner", "blocked_by", "trigger", "id"})


def _continuation_tags(lines: list[str], after: int) -> tuple[str, ...]:
    """Field tags on the continuation lines of the item at 1-based line `after`.

    Scanning starts on the line after the item's own: `lines` is 0-indexed, so
    `lines[after:]` with the 1-based `after` is exactly that.

    The shared parser is line-based, so a tag wrapped onto the next line scrapes
    as absent while its author believes the item is labelled — 2 of the 27
    "unowned" items on 2026-08-04 were exactly this (issue #37). Tokenizing is
    delegated to the shared scraper by re-reading each continuation as a one-line
    item, so what counts as a tag cannot drift from plan-fields."""
    found: list[str] = []
    for line in lines[after:]:
        stripped = line.strip()
        if not stripped or not line[0].isspace():
            break  # blank or a new top-level block: the item ended
        if scrape_items(line):
            break  # an indented checkbox is the next item, not a continuation
        probe = scrape_items(f"- [ ] {stripped}")[0]
        found.extend(k for k in probe.tags if k in _FIELD_KEYS and k not in found)
    return tuple(found)


def _plan_files(root: Path) -> Iterator[tuple[Path, str]]:
    """Readable plan files in one mirror, with their text.

    The single definition of "this repo has a plan": unreadable is treated as absent,
    for the scanner and the coverage check alike. Anything less — an existence test in
    one and a read in the other — lets them disagree about the same repo, which is the
    silent hole coverage_hit() exists to close."""
    for pattern in PLAN_GLOBS:
        for path in sorted(root.glob(pattern)):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            yield path, text


def open_items(config: RobinConfig) -> list[PlanItem]:
    """Every open plan item across the mirrors, unbudgeted, in repo-major order.

    The digest's prompt budget is applied downstream (digest.plan_hits); state and
    counters must see the complete set or the delta would report budget artefacts
    as movement."""
    items: list[PlanItem] = []
    for root in [config.vault_path, *config.repo_paths]:
        for path, text in _plan_files(root):
            rel = f"{root.name}/{path.relative_to(root)}"
            lines = text.splitlines()
            for item in scrape_items(text):
                if item.checked:
                    continue  # the digest's plan section is open work only
                # `display_text` is the item with every tag stripped: the checkbox
                # marker and the metadata read as noise once the item is quoted in
                # a sentence about movement. Identity is Robin's own (`_key`), not
                # the package's — kept unchanged until the fleet @id backfill.
                plan_text = item.display_text
                items.append(
                    PlanItem(
                        key=_key(rel, plan_text),
                        repo=root.name,
                        path=rel,
                        line=item.line,
                        heading=_section(item.section),
                        text=plan_text,
                        owner=item.tags.get("owner"),
                        blocked_by=item.tags.get("blocked_by"),
                        trigger=item.tags.get("trigger"),
                        hidden_tags=_continuation_tags(lines, item.line),
                    )
                )
    return items


def coverage_hit(config: RobinConfig) -> Hit | None:
    """Which mirrors have a plan file at all — None when every one of them does.

    A repo without a plan file contributes nothing to the "what remains" section and
    used to do so silently, which made a partial forward-look read as an ecosystem-wide
    one: on 2026-07-26 only 6 of 12 mirrors carried a plan file, 5 of them had anything
    open, and 56 of the 62 items came from two. An all-checked plan file counts as
    covered — that is a maintained plan that happens to be empty, not a missing one.

    Mirrors in `config.plan_exempt` are not expected to keep a plan (a knowledge base
    is not a project with work), so they leave the denominator instead of being
    reported as a gap every run — but the exemption is stated, because shrinking the
    denominator in silence is the failure this marker exists to catch."""
    mirrors = [config.vault_path, *config.repo_paths]
    names = {root.name for root in mirrors}
    # Only exemptions that actually matched may be claimed as applied: echoing a typo
    # as "exempt, not counted" while the real repo stays in the gap list would make
    # the honesty marker itself lie.
    applied = [name for name in config.plan_exempt if name in names]
    unknown = [name for name in config.plan_exempt if name not in names]
    expected = [root for root in mirrors if root.name not in applied]
    missing = [root.name for root in expected if next(_plan_files(root), None) is None]
    if not missing:
        return None
    exempted = (
        f" Exempt by configuration, not counted: {', '.join(applied)}."
        if applied
        else ""
    )
    if unknown:
        exempted += (
            f" Configured exemptions matching no mirror, ignored: {', '.join(unknown)}."
        )
    return Hit(
        "(plan-coverage)",
        1,
        f"plan files exist in {len(expected) - len(missing)} of {len(expected)} "
        f"mirrors; no plan file in: {', '.join(missing)}. Remaining work in those "
        "repos is INVISIBLE to this digest — they contribute nothing to any plan "
        "source, in either cadence, and that silence is not evidence that they have "
        f"nothing left to do.{exempted}",
    )


def _fleet_view(
    config: RobinConfig,
) -> tuple[ManifestIndex, dict[tuple[str, str, int], set[str]]]:
    """Resolve plan conditions once over Robin's frozen read-only mirror set."""
    roots = [config.vault_path, *config.repo_paths]
    names = frozenset(root.name.lower() for root in roots)
    index = ManifestIndex(names, {})
    inputs: list[RepoInput] = []
    source_lines: dict[tuple[str, int], tuple[str, str, int]] = {}
    for root in roots:
        chunks: list[str] = []
        next_line = 1
        for path, text in _plan_files(root):
            rel = f"{root.name}/{path.relative_to(root)}"
            for item in scrape_items(text):
                source_lines[(root.name.lower(), next_line + item.line - 1)] = (
                    root.name.lower(),
                    rel,
                    item.line,
                )
            chunks.append(text.rstrip("\r\n"))
            next_line += len(text.splitlines()) + 1
        inputs.append(
            RepoInput(
                root.name.lower(),
                todo_text="\n".join(chunks) or None,
                available=root.is_dir(),
            )
        )
    snapshot = parse_fleet(inputs, index)
    codes: dict[tuple[str, str, int], set[str]] = {}
    for diagnostic in [*snapshot["diagnostics"], *check_fleet(snapshot)]:
        provenance = diagnostic.get("provenance") or {}
        repo, line = provenance.get("repo"), provenance.get("line")
        if isinstance(repo, str) and isinstance(line, int):
            source = source_lines.get((repo, line))
            if source is not None:
                codes.setdefault(source, set()).add(diagnostic["code"])
    exclude = {
        (ref["provenance"]["repo"], ref["raw_ref"]) for ref in snapshot["references"]
    }
    for diagnostic in check_legacy_fleet(inputs, index, exclude=exclude):
        source = source_lines.get((diagnostic.source_repo, diagnostic.source_line))
        if source is not None:
            codes.setdefault(source, set()).add(diagnostic.code)
    return index, codes


def _ownership_bucket(owner: str | None, index: ManifestIndex) -> str:
    owner_ref, _owner_role, diagnostic = parse_owner(owner)
    if diagnostic == "PF-OWNER-MISSING":
        return "missing"
    if diagnostic is not None:
        return "invalid-owner"
    assert owner_ref is not None
    if owner_ref["kind"] in {"github_user", "github_team"}:
        return "human-owned"
    if owner_ref["kind"] == "tbd":
        return "TBD"
    if owner_ref["kind"] == "repository":
        return (
            "repo-owned"
            if owner_ref["id"] in index.canonical_keys
            else "unknown-repo-owner"
        )
    raise AssertionError(f"unexpected owner kind: {owner_ref['kind']}")


def _movement_bucket(item: PlanItem, codes: set[str]) -> str:
    if codes & _MALFORMED_CONDITION_CODES:
        return "malformed-condition"
    if "PF-BLOCKER-STALE" in codes or (item.blocked_by and "PF-LEGACY-STALE" in codes):
        return "stale-condition"
    if item.blocked_by:
        return "waiting-by-blocker"
    if item.trigger:
        return "waiting-by-trigger"
    return "actionable"


def fields_hit(config: RobinConfig, kind: str) -> Hit | None:
    """Typed ownership and independent movement totals for the open fleet plan."""
    items = open_items(config)
    if not items:
        return None
    index, codes = _fleet_view(config)
    ownership = {bucket: 0 for bucket in _OWNERSHIP}
    movement = {bucket: 0 for bucket in _MOVEMENT}
    matrix = {owner: {state: 0 for state in _MOVEMENT} for owner in _OWNERSHIP}
    for item in items:
        owner = _ownership_bucket(item.owner, index)
        state = _movement_bucket(
            item, codes.get((item.repo.lower(), item.path, item.line), set())
        )
        ownership[owner] += 1
        movement[state] += 1
        matrix[owner][state] += 1
    assert sum(ownership.values()) == len(items)
    assert sum(movement.values()) == len(items)
    missing_actionable = matrix["missing"]["actionable"]
    previous = load_state(config, kind)
    was_missing = ""
    if previous is not None and all("owner" in entry for entry in previous.values()):
        before = sum(
            parse_owner(entry.get("owner"))[2] == "PF-OWNER-MISSING"
            for entry in previous.values()
        )
        was_missing = f" (was missing={before} at the previous {kind} digest)"
    parts = [
        f"Ownership of {len(items)} open plan items: "
        + ", ".join(f"{name}={ownership[name]}" for name in _OWNERSHIP)
        + was_missing
        + ". Movement: "
        + ", ".join(f"{name}={movement[name]}" for name in _MOVEMENT)
        + "."
    ]
    if missing_actionable:
        parts.append(
            f"ACTION: {missing_actionable} item(s) are canonically both missing "
            "an owner and actionable; inspect these unattended next steps."
        )
    hidden = sum(bool(item.hidden_tags) for item in items)
    if hidden:
        parts.append(
            f"WARNING: {hidden} item(s) put field tags on continuation lines; "
            "the canonical line-based parser cannot see them."
        )
    return Hit("(plan-fields)", 1, " ".join(parts))


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
        items = state["items"]
        # Parsing is not validation: a snapshot that loads but has the wrong shape
        # would crash the next delta instead of degrading. A partial write costs one
        # cadence of comparison, which is the cheap failure — a crash is not.
        if state.get("version") != _STATE_VERSION or not isinstance(items, dict):
            raise ValueError(f"unsupported snapshot version/shape in {path}")
        for entry in items.values():
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("first_seen"), int)
                or not isinstance(entry.get("text"), str)
            ):
                raise ValueError(f"malformed snapshot entry in {path}")
        return dict(items)
    except (OSError, ValueError, KeyError, TypeError):
        logger.warning("unusable plan snapshot %s; treating as first run", path)
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
            # Always written, None included: the presence of the key is how a later
            # run knows this snapshot tracked owners and can be compared against.
            "owner": item.owner,
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
