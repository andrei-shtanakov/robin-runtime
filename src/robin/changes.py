"""Change-history retriever: git log over the read-only mirrors + vault journals.

Grounds "what changed today / this week / since <date>?" questions. Read-only by
construction: `git -C <repo> log` never touches the index or working tree. Results keep the
Hit/citation contract (path=repo@sha) so build_prompt() and the answer rules are unchanged.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import RobinConfig
from .kb import Hit

MAX_COMMITS_PER_REPO = 30
_GIT_TIMEOUT_S = 30


@dataclass(frozen=True)
class Period:
    """A half-open [since, until) window; until=None means 'now'."""

    since: datetime
    until: datetime | None
    label: str


# Ordered: more specific patterns first. RU + EN.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:позавчера|day before yesterday)\b", re.I), "day_before_yesterday"),
    (re.compile(r"\b(?:вчера|yesterday)\b", re.I), "yesterday"),
    (re.compile(r"\b(?:сегодня|за день|today)\b", re.I), "today"),
    (re.compile(r"\b(?:на этой неделе|за неделю|this week|за последнюю неделю|past week|last 7 days)\b", re.I), "week"),
    (re.compile(r"\b(?:на прошлой неделе|last week)\b", re.I), "last_week"),
    (re.compile(r"\b(?:за месяц|в этом месяце|this month|за последний месяц|past month)\b", re.I), "month"),
    (re.compile(r"\bпоследни[еихй]\s+(\d{1,3})\s+(?:дн|день|дня|дней)", re.I), "n_days"),
    (re.compile(r"\bза\s+(\d{1,3})\s+(?:дн|день|дня|дней)", re.I), "n_days"),
    (re.compile(r"\b(?:last|past)\s+(\d{1,3})\s+days?\b", re.I), "n_days"),
    (re.compile(r"\b(?:с|since|from)\s+(\d{4}-\d{2}-\d{2})\b", re.I), "since_date"),
]


def parse_period(text: str, *, tz: str = "UTC", now: datetime | None = None) -> Period | None:
    """Detect a change-window phrase in the question; None => not a period question."""
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for pattern, kind in _PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        if kind == "today":
            return Period(midnight, None, "today")
        if kind == "yesterday":
            return Period(midnight - timedelta(days=1), midnight, "yesterday")
        if kind == "day_before_yesterday":
            return Period(midnight - timedelta(days=2), midnight - timedelta(days=1), "day before yesterday")
        if kind == "week":
            return Period(now - timedelta(days=7), None, "past week")
        if kind == "last_week":
            start = midnight - timedelta(days=midnight.weekday() + 7)
            return Period(start, start + timedelta(days=7), "last week")
        if kind == "month":
            return Period(now - timedelta(days=30), None, "past month")
        if kind == "n_days":
            days = min(int(match.group(1)), 366)
            return Period(now - timedelta(days=days), None, f"last {days} days")
        if kind == "since_date":
            since = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=zone)
            return Period(since, None, f"since {match.group(1)}")
    return None


@dataclass(frozen=True)
class Commit:
    sha: str
    date: str
    author: str
    subject: str
    stat: str  # "N files changed, +A/-B" summary line


def git_log(repo, since: datetime, until: datetime | None) -> list[Commit]:
    """Read-only `git log` over one mirror; empty list when git fails or repo is not one."""
    args = [
        "git", "-C", str(repo), "log", "--no-merges", "--date=iso-strict",
        f"--since={since.isoformat()}", "--shortstat",
        "--pretty=format:%x1e%h%x1f%ad%x1f%an%x1f%s",
        f"--max-count={MAX_COMMITS_PER_REPO}",
    ]
    if until is not None:
        args.append(f"--until={until.isoformat()}")
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits: list[Commit] = []
    for chunk in proc.stdout.split("\x1e"):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, stat = chunk.partition("\n")
        fields = head.split("\x1f")
        if len(fields) != 4:
            continue
        sha, date, author, subject = fields
        commits.append(Commit(sha, date[:10], author, subject, stat.strip()))
    return commits


def journal_entries(config: RobinConfig, period: Period, *, max_hits: int = 10) -> list[Hit]:
    """Vault journal lines dated inside the period (derived/journal/* uses date headings)."""
    journal_dir = config.vault_path / "derived" / "journal"
    if not journal_dir.is_dir():
        return []
    date_re = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
    since = period.since.date()
    until = period.until.date() if period.until else None
    hits: list[Hit] = []
    for path in sorted(journal_dir.rglob("*.md")):
        try:
            lines = path.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        current_in_period = False
        for number, line in enumerate(lines, 1):
            match = date_re.search(line)
            if match:
                day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                current_in_period = day >= since and (until is None or day < until)
            if current_in_period and line.strip():
                rel = path.relative_to(config.vault_path)
                hits.append(Hit(str(rel), number, line.strip()[:240]))
                if len(hits) >= max_hits:
                    return hits
    return hits


def collect_changes(config: RobinConfig, period: Period, *, max_hits: int = 40) -> list[Hit]:
    """Change evidence for the period: commits per mirror (as repo@sha hits) + journals."""
    hits: list[Hit] = []
    repos = [config.vault_path, *config.repo_paths]
    for repo in repos:
        for commit in git_log(repo, period.since, period.until):
            text = f"{commit.date} {commit.author}: {commit.subject}"
            if commit.stat:
                text += f" ({commit.stat})"
            hits.append(Hit(f"{repo.name}@{commit.sha}", 1, text[:240]))
    if not hits:
        hits.append(
            Hit("(no-commits)", 1, f"No commits found in any mirror for period: {period.label}")
        )
    hits += journal_entries(config, period)
    return hits[:max_hits]


def _main() -> None:
    import sys

    from .config import load_config

    config = load_config()
    text = " ".join(sys.argv[1:]) or "this week"
    period = parse_period(text, tz=config.tz)
    if period is None:
        print(f"no period detected in: {text!r}")
        return
    print(f"period: {period.label} (since {period.since:%Y-%m-%d %H:%M})")
    for hit in collect_changes(config, period):
        print(f"  {hit.path}: {hit.text}")


if __name__ == "__main__":
    _main()
