"""Period parsing (RU+EN) and read-only git-history retrieval over throwaway repos."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from robin.changes import Period, collect_changes, git_log, parse_period
from robin.config import RobinConfig

NOW = datetime(2026, 7, 9, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("что изменилось сегодня?", "today"),
        ("what changed today", "today"),
        ("что поменялось за день", "today"),
        ("что нового за неделю?", "past week"),
        ("what changed this week?", "past week"),
        ("итоги за последнюю неделю", "past week"),
        ("что было вчера", "yesterday"),
        ("what happened yesterday", "yesterday"),
        ("изменения за месяц", "past month"),
        ("what changed this month", "past month"),
        ("что нового за 3 дня", "last 3 days"),
        ("last 10 days of changes", "last 10 days"),
        ("what changed since 2026-07-01", "since 2026-07-01"),
        ("что изменилось с 2026-07-01", "since 2026-07-01"),
    ],
)
def test_parse_period_matrix(text: str, label: str) -> None:
    period = parse_period(text, tz="UTC", now=NOW)
    assert period is not None, text
    assert period.label == label


@pytest.mark.parametrize(
    "text",
    [
        "what is the arbiter repo for?",
        "зачем нужен репозиторий maestro?",
        "who owns the agents-catalog?",
    ],
)
def test_non_period_questions_pass_through(text: str) -> None:
    assert parse_period(text, tz="UTC", now=NOW) is None


def test_yesterday_is_a_closed_window() -> None:
    period = parse_period("вчера", tz="UTC", now=NOW)
    assert period.since == datetime(2026, 7, 8, tzinfo=timezone.utc)
    assert period.until == datetime(2026, 7, 9, tzinfo=timezone.utc)


def _make_repo(path: Path, commits: list[tuple[str, str]]) -> None:
    """(iso_date, subject) commits with deterministic dates."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    }
    for index, (date, subject) in enumerate(commits):
        (path / f"f{index}.txt").write_text(subject)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True, env=env)
        stamp_env = {**env, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        subprocess.run(
            ["git", "-C", str(path), "commit", "-q", "-m", subject], check=True, env=stamp_env
        )


def test_git_log_filters_by_window(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _make_repo(
        repo,
        [
            ("2026-07-01T10:00:00+00:00", "old change"),
            ("2026-07-08T10:00:00+00:00", "fresh change"),
        ],
    )
    since = datetime(2026, 7, 7, tzinfo=timezone.utc)
    commits = git_log(repo, since, None)
    assert [c.subject for c in commits] == ["fresh change"]
    assert commits[0].date == "2026-07-08"
    assert "1 file changed" in commits[0].stat


def test_git_log_survives_non_repo(tmp_path: Path) -> None:
    assert git_log(tmp_path, NOW, None) == []


def test_collect_changes_cites_repo_at_sha(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_repo(vault, [("2026-07-08T10:00:00+00:00", "vault update")])
    repo = tmp_path / "arbiter"
    _make_repo(repo, [("2026-07-08T11:00:00+00:00", "routing fix")])
    config = RobinConfig(vault_path=vault, repo_paths=[repo], var_dir=tmp_path / "var")
    period = Period(since=datetime(2026, 7, 7, tzinfo=timezone.utc), until=None, label="test")
    hits = collect_changes(config, period)
    paths = [hit.path for hit in hits]
    assert any(p.startswith("vault@") for p in paths)
    assert any(p.startswith("arbiter@") for p in paths)
    assert any("routing fix" in hit.text for hit in hits)


def test_collect_changes_reports_empty_window(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _make_repo(vault, [("2026-01-01T10:00:00+00:00", "ancient")])
    config = RobinConfig(vault_path=vault, repo_paths=[], var_dir=tmp_path / "var")
    period = Period(since=datetime(2026, 7, 7, tzinfo=timezone.utc), until=None, label="test")
    hits = collect_changes(config, period)
    assert hits[0].path == "(no-commits)"
