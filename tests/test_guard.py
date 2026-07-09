"""§7 budget/rate-limit math over fixture JSONL, including day boundaries and TZ."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from robin.config import RobinConfig
from robin.guard import (
    BudgetExceeded,
    RateLimited,
    check,
    cost_report,
    requests_today,
    spent_today,
)

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


def _write_log(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _ts(hour: int, day: int = 9) -> int:
    return int(datetime(2026, 7, day, hour, 0, tzinfo=timezone.utc).timestamp())


def test_spent_today_ignores_yesterday_and_bad_lines(tmp_path: Path) -> None:
    log = tmp_path / "interactions.jsonl"
    _write_log(
        log,
        [
            {"ts": _ts(10), "cost_usd": 0.5, "requester": "1"},
            {"ts": _ts(23, day=8), "cost_usd": 9.0, "requester": "1"},  # yesterday
            {"ts": _ts(11), "cost_usd": None, "requester": "1"},  # retrieve-only
        ],
    )
    log.open("a").write("not json\n")
    assert spent_today(log, tz="UTC", now=NOW) == pytest.approx(0.5)


def test_requests_today_counts_per_requester(tmp_path: Path) -> None:
    log = tmp_path / "interactions.jsonl"
    _write_log(
        log,
        [
            {"ts": _ts(10), "requester": "42"},
            {"ts": _ts(11), "requester": "42"},
            {"ts": _ts(11), "requester": "7"},
        ],
    )
    assert requests_today(log, "42", tz="UTC", now=NOW) == 2
    assert requests_today(log, "7", tz="UTC", now=NOW) == 1


def _config(tmp_path: Path, **overrides) -> RobinConfig:
    defaults = dict(
        vault_path=tmp_path, repo_paths=[], var_dir=tmp_path,
        daily_budget_usd=1.0, user_daily_msgs=2,
    )
    return RobinConfig(**{**defaults, **overrides})


def test_check_raises_budget_exceeded(tmp_path: Path) -> None:
    _write_log(tmp_path / "interactions.jsonl", [{"ts": _ts(10), "cost_usd": 1.5}])
    with pytest.raises(BudgetExceeded):
        check(_config(tmp_path), "42", now=NOW)


def test_check_raises_rate_limited(tmp_path: Path) -> None:
    _write_log(
        tmp_path / "interactions.jsonl",
        [{"ts": _ts(10), "requester": "42"}, {"ts": _ts(11), "requester": "42"}],
    )
    with pytest.raises(RateLimited):
        check(_config(tmp_path), "42", now=NOW)
    check(_config(tmp_path), "7", now=NOW)  # other users unaffected


def test_check_passes_on_fresh_day(tmp_path: Path) -> None:
    check(_config(tmp_path), "42", now=NOW)  # no log at all


def test_tz_day_boundary(tmp_path: Path) -> None:
    # 23:30 UTC on Jul 8 is already "today" (Jul 9) in UTC+3.
    log = tmp_path / "interactions.jsonl"
    late = int(datetime(2026, 7, 8, 23, 30, tzinfo=timezone.utc).timestamp())
    _write_log(log, [{"ts": late, "cost_usd": 0.7}])
    now = datetime(2026, 7, 9, 6, 0, tzinfo=timezone.utc)
    assert spent_today(log, tz="Europe/Moscow", now=now) == pytest.approx(0.7)
    assert spent_today(log, tz="UTC", now=now) == 0.0


def test_cost_report_mentions_budget_and_users(tmp_path: Path) -> None:
    _write_log(
        tmp_path / "interactions.jsonl",
        [{"ts": _ts(10), "cost_usd": 0.25, "requester": "42"}],
    )
    report = cost_report(_config(tmp_path), now=NOW)
    assert "$0.2500" in report and "$1.00" in report and "42: 1/2" in report
