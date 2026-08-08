"""Independent reader of steward's arch-evidence-freshness runs (issue #42)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from robin.config import RobinConfig
from robin.freshness import FRESH_HOURS, freshness_hit, verdict_text

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _run(conclusion: str = "success", *, hours_ago: int = 2, status: str = "completed"):
    finished = NOW.timestamp() - hours_ago * 3600
    stamp = datetime.fromtimestamp(finished, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {"status": status, "conclusion": conclusion, "updated_at": stamp}


def _config(tmp_path: Path, repo: str = "org/steward") -> RobinConfig:
    return RobinConfig(
        vault_path=tmp_path,
        repo_paths=[],
        var_dir=tmp_path / "var",
        freshness_repo=repo,
    )


def test_fresh_success_is_clean() -> None:
    text = verdict_text([_run(hours_ago=6)], repo="steward", now=NOW)
    assert "clean" in text
    assert "6 hours ago" in text


def test_stale_success_is_silent_schedule_not_clean() -> None:
    # GitHub disables cron workflows after 60 idle days, and an Actions outage looks
    # identical — a stale success means the schedule is silent, never "still fine".
    text = verdict_text([_run(hours_ago=FRESH_HOURS + 10)], repo="steward", now=NOW)
    assert "SILENT" in text and "UNKNOWN" in text
    assert "clean:" not in text


def test_failed_run_is_not_clean_and_points_at_the_status_artifact() -> None:
    text = verdict_text([_run("failure", hours_ago=3)], repo="steward", now=NOW)
    assert "NOT" in text and "failure" in text
    # non-clean details live in the run's artifact (arch-evidence-freshness-run/v1)
    assert "arch-evidence-freshness-status" in text


def test_latest_completed_run_wins_over_a_pending_one() -> None:
    runs = [_run("", hours_ago=0, status="in_progress"), _run(hours_ago=4)]
    text = verdict_text(runs, repo="steward", now=NOW)
    assert "clean" in text


def test_no_completed_runs_is_unknown() -> None:
    for runs in ([], [_run("", hours_ago=0, status="queued")]):
        text = verdict_text(runs, repo="steward", now=NOW)
        assert "UNKNOWN" in text


def test_unreadable_api_is_unknown_never_silence() -> None:
    # A fetch failure must surface as an explicit unknown line — dropping the source
    # would make an Actions outage indistinguishable from a healthy quiet day.
    text = verdict_text(None, repo="steward", now=NOW)
    assert "could not read" in text and "UNKNOWN" in text


def test_bad_timestamp_is_unknown() -> None:
    run = {"status": "completed", "conclusion": "success", "updated_at": "not-a-date"}
    text = verdict_text([run], repo="steward", now=NOW)
    assert "UNKNOWN" in text


def test_hit_carries_the_verdict_and_names_the_source(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("robin.freshness._fetch_runs", lambda repo: [_run(hours_ago=1)])
    hit = freshness_hit(_config(tmp_path), now=NOW)
    assert hit is not None
    assert hit.path == "(arch-evidence-freshness)"
    assert "clean" in hit.text


def test_empty_repo_disables_the_reader(tmp_path) -> None:
    assert freshness_hit(_config(tmp_path, repo=""), now=NOW) is None


def test_both_digests_carry_the_freshness_verdict(tmp_path, monkeypatch) -> None:
    from robin.digest import compose

    monkeypatch.setattr(
        "robin.digest._compose_answer", lambda *args, **kwargs: ("digest text", 0.0)
    )
    monkeypatch.setattr(
        "robin.freshness._fetch_runs", lambda repo: [_run("failure", hours_ago=2)]
    )
    for kind in ("daily", "weekly"):
        _, sources, _ = compose(_config(tmp_path), kind, now=NOW)
        marks = [hit for hit in sources if hit.path == "(arch-evidence-freshness)"]
        assert len(marks) == 1 and "NOT" in marks[0].text


def test_digest_without_freshness_repo_makes_no_fetch(tmp_path, monkeypatch) -> None:
    from robin.digest import compose

    monkeypatch.setattr(
        "robin.digest._compose_answer", lambda *args, **kwargs: ("digest text", 0.0)
    )

    def _explode(repo):
        raise AssertionError("network reader called with the feature disabled")

    monkeypatch.setattr("robin.freshness._fetch_runs", _explode)
    _, sources, _ = compose(_config(tmp_path, repo=""), "daily", now=NOW)
    assert not any(hit.path == "(arch-evidence-freshness)" for hit in sources)
