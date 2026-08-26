"""Epic-axis shadow digest (ADR-ECO-010 Ф5, slice 1): deterministic, never posted.

Spec: docs/superpowers/specs/2026-08-26-epic-shadow-digest-design.md. The shadow
renders the SAME weekly Period the digest was composed over, grouped by epic from
commit trailers (git's own trailer semantics, not grep) plus the live registry
read from the umbrella mirror. No LLM call: this is a measuring instrument for
the cutover decision (D8), so its output must be byte-reproducible. Failure of
the shadow must never affect the digest — run() wraps the call (§3.1).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .changes import Period
from .config import RobinConfig

logger = logging.getLogger("robin.epic_shadow")

_GIT_TIMEOUT_S = 30
_REGISTRY_REPO = "ai-orchestrators-workspace"
_REGISTRY_FILE = "epics.toml"
_EXAMPLES_MAX = 3
# Records by 0x1e, fields by 0x1f — the changes.py collector's framing: trailer
# values may be multiline, so newlines cannot delimit either records or fields.
_LOG_FORMAT = (
    "%x1e%H%x1f%(trailers:key=Epic,valueonly=true)"
    "%x1f%(trailers:key=Defect,valueonly=true)"
)

# The two declared blind spots of the trailer source (spec §4); slice 2 closes
# both via the snapshot/v2 merged window (todo://robin-runtime/epic-shadow-pr-attribution).
_KNOWN_INCOMPLETENESS = (
    "branch commits older than the window but merged inside it are not seen "
    "by `git log --since`",
    "a trailer living only in the PR body (squash without carrying it into "
    "the commit message) is not recoverable from git history",
)


@dataclass(frozen=True)
class ShadowSnapshot:
    """One computed slice — both artifacts render from this single object."""

    window: dict[str, str]
    generated_at: str  # ISO of period.until, NOT wall clock: determinism (§6)
    per_epic: dict[str, dict[str, Any]]
    buckets: dict[str, dict[str, Any]]
    provenance: dict[str, Any] = field(default_factory=dict)


def _load_registry(
    config: RobinConfig,
) -> tuple[dict[str, str | None] | None, str]:
    """{epic id: title|None} from the umbrella mirror, or (None, reason).

    All-or-nothing by design (§3.2): ANY failure to obtain the key set — mirror
    absent, unreadable file, decode error, TOML error, missing or non-table
    [epics] — yields `unavailable`, never a crash and never a silently empty
    registry (an empty registry would present the missing instrument as a
    finding: every key would read as `unregistered`).
    """
    mirror = next((p for p in config.repo_paths if p.name == _REGISTRY_REPO), None)
    if mirror is None:
        return None, "mirror not present"
    try:
        data = tomllib.loads((mirror / _REGISTRY_FILE).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — the catch-all IS the contract here
        return None, f"{type(exc).__name__}: {exc}"
    epics = data.get("epics")
    if not isinstance(epics, dict):
        return None, "[epics] missing or not a table"
    registry: dict[str, str | None] = {}
    for key, entry in epics.items():
        title = entry.get("title") if isinstance(entry, dict) else None
        registry[key] = title if isinstance(title, str) else None
    return registry, "read"


def _log_trailers(
    repo: Path, period: Period
) -> tuple[list[tuple[str, list[str], bool]], int] | str:
    """[(sha, unique epic values, has defect)] + unparsed count, or skip reason."""
    assert period.until is not None  # run() pins the top (§3.1)
    args = [
        "git",
        "-C",
        str(repo),
        "log",
        # no --no-merges (unlike changes.git_log): the shadow counts every
        # commit of the window; a merge commit without trailers is honestly
        # unclassified (§4).
        f"--since={period.since.isoformat()}",
        f"--until={period.until.isoformat()}",
        f"--pretty=format:{_LOG_FORMAT}",
    ]
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return "skipped: timeout"
    except OSError as exc:
        return f"skipped: {type(exc).__name__}"
    if proc.returncode != 0:
        return f"skipped: git exited {proc.returncode}"
    commits: list[tuple[str, list[str], bool]] = []
    unparsed = 0
    for chunk in proc.stdout.split("\x1e"):
        if not chunk.strip():
            continue
        fields = chunk.split("\x1f")
        if len(fields) != 3:
            unparsed += 1
            continue
        sha, epics_raw, defects_raw = fields
        epics = sorted(
            {value.strip() for value in epics_raw.split("\n") if value.strip()}
        )
        has_defect = any(value.strip() for value in defects_raw.split("\n"))
        commits.append((sha.strip(), epics, has_defect))
    return commits, unparsed


def collect(config: RobinConfig, period: Period) -> ShadowSnapshot:
    """Walk [vault, *mirrors], classify each commit of the window (§4)."""
    if period.until is None:
        raise ValueError("epic shadow requires a window with a pinned until")
    registry, registry_state = _load_registry(config)
    unknown_bucket = "unregistered" if registry is not None else "unverified"

    per_epic: dict[str, dict[str, Any]] = {}
    buckets: dict[str, dict[str, Any]] = {
        "unclassified": {"commits": 0},
        unknown_bucket: {"commits": 0, "examples": []},
        "conflict": {"commits": 0, "examples": []},
    }
    mirrors: dict[str, str] = {
        name: "skipped: not a directory" for name in config.missing_mirrors
    }

    for repo in sorted([config.vault_path, *config.repo_paths], key=lambda p: p.name):
        result = _log_trailers(repo, period)
        if isinstance(result, str):
            mirrors[repo.name] = result
            continue
        commits, unparsed = result
        mirrors[repo.name] = f"partial: {unparsed} unparsed" if unparsed else "read"
        for sha, epics, has_defect in commits:
            if len(epics) >= 2:
                bucket = buckets["conflict"]
            elif not epics:
                bucket = buckets["unclassified"]
            elif registry is not None and epics[0] in registry:
                row = per_epic.setdefault(
                    epics[0],
                    {
                        "title": registry[epics[0]],
                        "commits": 0,
                        "defects": 0,
                        "repos": [],
                    },
                )
                row["commits"] += 1
                row["defects"] += 1 if has_defect else 0
                if repo.name not in row["repos"]:
                    row["repos"].append(repo.name)
                continue
            else:
                bucket = buckets[unknown_bucket]
            bucket["commits"] += 1
            if "examples" in bucket:
                bucket["examples"].append(
                    {"repo": repo.name, "sha": sha, "keys": epics}
                )

    for row in per_epic.values():
        row["repos"].sort()
    for bucket in buckets.values():
        if "examples" in bucket:
            bucket["examples"] = sorted(
                bucket["examples"], key=lambda e: (e["repo"], e["sha"])
            )[:_EXAMPLES_MAX]

    return ShadowSnapshot(
        window={
            "since": period.since.isoformat(),
            "until": period.until.isoformat(),
            "label": period.label,
        },
        generated_at=period.until.isoformat(),
        per_epic=per_epic,
        buckets=buckets,
        provenance={
            "mirrors": mirrors,
            "registry": registry_state
            if registry is not None
            else f"unavailable: {registry_state}",
            "known_incompleteness": list(_KNOWN_INCOMPLETENESS),
        },
    )


def render_json(snapshot: ShadowSnapshot) -> str:
    return json.dumps(asdict(snapshot), sort_keys=True, ensure_ascii=False, indent=2)


def _example_line(example: dict[str, Any]) -> str:
    # Raw values are rendered as JSON string literals: unambiguous escaping
    # without inventing rules — ["a+b","c"] and ["a","b+c"] stay distinct (§6).
    keys = " + ".join(json.dumps(key, ensure_ascii=False) for key in example["keys"])
    return f"{example['repo']}@{example['sha'][:12]} {keys}"


def render_md(snapshot: ShadowSnapshot) -> str:
    lines = [
        f"# Weekly epic-axis shadow — {snapshot.generated_at[:10]}",
        "",
        f"generated_at: {snapshot.generated_at}",
        f"window: {snapshot.window['since']} → {snapshot.window['until']}",
        "",
        "## Эпики",
        "",
    ]
    for key in sorted(snapshot.per_epic):
        row = snapshot.per_epic[key]
        title = f" «{row['title']}»" if row["title"] else ""
        lines.append(
            f"- {key}{title} — {row['commits']} коммитов, "
            f"{row['defects']} с Defect:, репо: {', '.join(row['repos'])}"
        )
    if not snapshot.per_epic:
        lines.append("- (классифицированных коммитов нет)")
    lines += ["", "## Bucket-строки", ""]
    for name in sorted(snapshot.buckets):
        bucket = snapshot.buckets[name]
        line = f"- {name} — {bucket['commits']} коммитов"
        if bucket.get("examples"):
            examples = "; ".join(_example_line(e) for e in bucket["examples"])
            line += f"; примеры: {examples}"
        lines.append(line)
    lines += ["", "## Провенанс", ""]
    for name in sorted(snapshot.provenance["mirrors"]):
        lines.append(f"- {name}: {snapshot.provenance['mirrors'][name]}")
    lines.append(f"- registry: {snapshot.provenance['registry']}")
    for note in snapshot.provenance["known_incompleteness"]:
        lines.append(f"- заявленная неполнота: {note}")
    return "\n".join(lines) + "\n"


def _write_verified(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if path.read_text() != content:  # §6.4 read-back, same as digest.persist
        raise RuntimeError(f"read-back verification failed for {path}")


def persist_shadow(config: RobinConfig, snapshot: ShadowSnapshot) -> tuple[Path, Path]:
    """JSON first, then MD, both from the one snapshot; same-date reruns overwrite.

    The pair carries the same generated_at label; a mixed pair (one write
    failed on a same-day rerun) is detectable by label mismatch (§6).
    """
    date = snapshot.generated_at[:10]
    json_path = config.var_dir / "epic-shadow" / f"{date}.json"
    md_path = config.var_dir / "digests" / f"{date}-weekly-epic-shadow.md"
    _write_verified(json_path, render_json(snapshot))
    _write_verified(md_path, render_md(snapshot))
    return json_path, md_path


def record_failure(config: RobinConfig, error: str) -> None:
    """Best-effort failure record: its own failure is swallowed (§3.1) — with
    var/ itself unreadable neither artifacts nor the record can land, and the
    signal is the systemd log plus the missing files."""
    try:
        config.var_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(time.time()),
            "surface": "epic-shadow",
            "kind": "weekly",
            "ok": False,
            "error": error,
        }
        with (config.var_dir / "interactions.jsonl").open("a") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — deliberately silent, see docstring
        logger.exception("epic-shadow failure record could not be written")


def run_shadow(config: RobinConfig, period: Period) -> tuple[Path, Path]:
    """Collect + persist one shadow slice; caller (digest.run) isolates errors."""
    snapshot = collect(config, period)
    json_path, md_path = persist_shadow(config, snapshot)
    logger.info("epic shadow persisted: %s, %s", json_path, md_path)
    return json_path, md_path
