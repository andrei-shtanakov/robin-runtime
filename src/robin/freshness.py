"""Independent reader of steward's arch-evidence-freshness scheduled run (issue #42).

The arch-evidence watch runs as a daily cron workflow in steward, and a scheduler
cannot report its own silence: GitHub disables cron workflows after 60 idle days, and
an Actions outage looks exactly like a quiet day. Robin is driven by its own systemd
timer — the independent second clock — so each digest reads the LAST run's machine
verdict from the public GitHub API. Robin never recomputes the sensor's semantics
(same rule as the fleet plan-check); non-clean details live in the run's
`arch-evidence-freshness-status` artifact (envelope `arch-evidence-freshness-run/v1`).
A fetch failure is reported as an explicit UNKNOWN line, never dropped: silence about
silence is the failure mode this reader exists to close."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

from .config import RobinConfig
from .kb import Hit

logger = logging.getLogger("robin.freshness")

WORKFLOW = "arch-evidence-freshness.yml"
# 24 h cadence + observed queue delay of 40–70 min + margin (issue #42): a success
# older than this means the schedule is silent, not that everything is fine.
FRESH_HOURS = 30
_TIMEOUT_S = 10


def freshness_hit(config: RobinConfig, *, now: datetime | None = None) -> Hit | None:
    """One digest source line with the watch verdict; None when the reader is off."""
    if not config.freshness_repo:
        return None
    now = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
    repo = config.freshness_repo.rpartition("/")[2]
    text = verdict_text(_fetch_runs(config.freshness_repo), repo=repo, now=now)
    return Hit("(arch-evidence-freshness)", 1, text)


def verdict_text(runs: list[dict] | None, *, repo: str, now: datetime) -> str:
    """Verdict over the newest-first run list; None means the API was unreadable."""
    unknown = f"architecture-evidence freshness in {repo} is UNKNOWN, not clean"
    if runs is None:
        return (
            f"could not read the {WORKFLOW} workflow runs from the GitHub API — "
            f"{unknown}."
        )
    completed = [run for run in runs if run.get("status") == "completed"]
    if not completed:
        return (
            f"no completed {WORKFLOW} runs are visible in {repo} — the schedule "
            f"looks SILENT and {unknown}."
        )
    last = completed[0]
    try:
        finished = datetime.fromisoformat(str(last.get("updated_at", "")))
    except ValueError:
        return f"the last {WORKFLOW} run in {repo} has no readable finish time — {unknown}."
    hours = int((now - finished).total_seconds() // 3600)
    if last.get("conclusion") != "success":
        return (
            f"the last {WORKFLOW} run in {repo} did NOT succeed (conclusion: "
            f"{last.get('conclusion')}, {hours} hours ago) — architecture evidence "
            "is not confirmed clean; details are in the run's "
            "arch-evidence-freshness-status artifact."
        )
    if hours > FRESH_HOURS:
        return (
            f"the {WORKFLOW} schedule in {repo} is SILENT: the last successful run "
            f"finished {hours} hours ago, beyond the {FRESH_HOURS}-hour window — "
            f"{unknown} (a disabled cron or an Actions outage looks exactly like this)."
        )
    return (
        f"arch-evidence-freshness watch in {repo} is clean: the last run succeeded "
        f"{hours} hours ago."
    )


def _fetch_runs(repo_slug: str) -> list[dict] | None:
    """Newest-first runs of the watch workflow; None on any read failure."""
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repo_slug}/actions/workflows/{WORKFLOW}"
        "/runs?per_page=5",
        headers={
            "Accept": "application/vnd.github+json",
            # the GitHub API rejects requests without a User-Agent
            "User-Agent": "robin-runtime-freshness-reader",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            payload = json.load(response)
        runs = payload["workflow_runs"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        logger.warning("freshness read failed for %s: %s", repo_slug, exc)
        return None
    return runs if isinstance(runs, list) else None
