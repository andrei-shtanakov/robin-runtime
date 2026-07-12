"""§7 cost controls: daily budget cap, per-user rate limit, /cost report.

Reads the append-only interaction log (agent._log). A linear scan of one day's JSONL is
fine at this scale; SQLite is the M3+ upgrade path (slot 4).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import RobinConfig


class BudgetExceeded(Exception):
    """Daily USD budget is spent; refuse until the local-midnight reset."""


class RateLimited(Exception):
    """This requester hit their daily message quota."""


def _day_bounds(tz: str, now: datetime | None = None) -> tuple[int, int]:
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else datetime.now(zone)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp()), int(now.timestamp())


def _today_records(log_path: Path, tz: str, now: datetime | None = None) -> list[dict]:
    if not log_path.is_file():
        return []
    start, end = _day_bounds(tz, now)
    records: list[dict] = []
    with log_path.open() as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = record.get("ts")
            if isinstance(ts, int) and start <= ts <= end:
                records.append(record)
    return records


def spent_today(
    log_path: Path, *, tz: str = "UTC", now: datetime | None = None
) -> float:
    """Total USD spent today (sum of logged cost_usd)."""
    return sum(
        record["cost_usd"]
        for record in _today_records(log_path, tz, now)
        if isinstance(record.get("cost_usd"), (int, float))
    )


def requests_today(
    log_path: Path, requester: str, *, tz: str = "UTC", now: datetime | None = None
) -> int:
    """How many interactions this requester logged today."""
    return sum(
        1
        for record in _today_records(log_path, tz, now)
        if record.get("requester") == requester
    )


def check(config: RobinConfig, requester: str, *, now: datetime | None = None) -> None:
    """Raise BudgetExceeded / RateLimited when §7 caps are hit. Call BEFORE the LLM."""
    log_path = config.var_dir / "interactions.jsonl"
    if spent_today(log_path, tz=config.tz, now=now) >= config.daily_budget_usd:
        raise BudgetExceeded(
            f"daily budget ${config.daily_budget_usd:.2f} reached; resets at local midnight"
        )
    if (
        requests_today(log_path, requester, tz=config.tz, now=now)
        >= config.user_daily_msgs
    ):
        raise RateLimited(f"daily quota of {config.user_daily_msgs} messages reached")


def cost_report(config: RobinConfig, *, now: datetime | None = None) -> str:
    """Human-readable /cost summary: today's spend, budget, per-user counts."""
    log_path = config.var_dir / "interactions.jsonl"
    records = _today_records(log_path, config.tz, now)
    spent = sum(
        r["cost_usd"] for r in records if isinstance(r.get("cost_usd"), (int, float))
    )
    by_user: dict[str, int] = {}
    for record in records:
        requester = record.get("requester") or "(anonymous)"
        by_user[requester] = by_user.get(requester, 0) + 1
    lines = [
        f"Today: ${spent:.4f} of ${config.daily_budget_usd:.2f} budget, "
        f"{len(records)} interactions.",
    ]
    lines += [
        f"  {user}: {count}/{config.user_daily_msgs} msgs"
        for user, count in sorted(by_user.items(), key=lambda kv: -kv[1])
    ]
    return "\n".join(lines)
