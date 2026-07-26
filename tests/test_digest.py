"""Digest windowing/persistence (§6.4 read-back) and §7 liveness staleness."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from robin.config import RobinConfig
from robin.digest import (
    compose,
    latest,
    persist,
    plan_hits,
    run,
    watched_repos_hit,
    window,
)
from robin.liveness import stale_kinds

NOW = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path, grace: int = 6) -> RobinConfig:
    return RobinConfig(
        vault_path=tmp_path,
        repo_paths=[],
        var_dir=tmp_path / "var",
        digest_grace_hours=grace,
    )


def test_window_falls_back_to_cadence(tmp_path: Path) -> None:
    period = window(_config(tmp_path), "daily", now=NOW)
    assert (NOW - period.since).total_seconds() == 24 * 3600


def test_window_resumes_from_marker_and_persist_writes_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = persist(config, "daily", "digest body", now=NOW)
    assert path.is_file() and "digest body" in path.read_text()
    period = window(config, "daily", now=NOW)
    assert period.since == NOW  # marker beats cadence fallback


def test_latest_returns_newest_digests_truncated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert latest(config) == []  # no digests yet — ambient context degrades gracefully
    persist(config, "weekly", "old week " + "x" * 5000, now=NOW)
    persist(config, "daily", "fresh\nmulti-line day", now=NOW.replace(day=10))
    excerpts = latest(config, limit=2, max_chars=100)
    assert len(excerpts) == 2
    assert excerpts[0].startswith("2026-07-10-daily.md:")  # newest first
    assert all(len(e) <= len("2026-07-09-weekly.md: ") + 100 for e in excerpts)
    # one prompt bullet per digest — persisted markdown is flattened
    assert all("\n" not in e for e in excerpts)
    assert "fresh multi-line day" in excerpts[0]


def test_plan_hits_collects_only_unchecked_items(tmp_path: Path) -> None:
    repo = tmp_path / "maestro"
    (repo / "docs" / "plans").mkdir(parents=True)
    (repo / "TODO.md").write_text(
        "# Plan\n- [x] shipped thing\n- [ ] open thing\n* [ ] starred open thing\n- free-form note\n"
    )
    (repo / "docs" / "plans" / "m5.md").write_text("- [ ] milestone step\n")
    config = RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )
    hits = plan_hits(config)
    texts = [hit.text for hit in hits]
    assert len(hits) == 2
    # the enclosing section heading is carried as plain-language context
    assert all(text.startswith("open plan item (Plan): ") for text in texts)
    assert not any("shipped thing" in text for text in texts)
    assert not any("free-form note" in text for text in texts)
    # docs/plans/*.md are implementation micro-steps, not team-level plan items
    assert not any("milestone step" in text for text in texts)
    assert hits[0].path == "maestro/TODO.md" and hits[0].line == 3


def test_plan_hits_tracks_current_heading(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "TODO.md").write_text(
        "- [ ] before any heading\n"
        "## **Судья (judge)**\n"
        "- [ ] P4 + prefill\n"
        "### Phase-1b\n"
        "- [ ] ablation ticket\n"
    )
    config = RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )
    texts = [hit.text for hit in plan_hits(config)]
    assert texts[0].startswith("open plan item: ")  # no heading yet — plain label
    assert texts[1].startswith("open plan item (Судья (judge)): ")  # ** stripped
    assert texts[2].startswith("open plan item (Phase-1b): ")  # nearest heading wins


def test_plan_hits_caps_and_degrades_without_plans(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert plan_hits(config) == []  # no plan files anywhere — section grounding empty
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "TODO.md").write_text("\n".join("- [ ] item" for _ in range(30)))
    config = RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )
    hits = plan_hits(config, max_hits=5)
    # truncation is disclosed, never silent (incident 2026-07-16)
    assert len(hits) == 6
    assert hits[-1].path == "(plan-items-truncated)"
    assert "5 of 30" in hits[-1].text
    # under the cap: no marker
    assert plan_hits(config, max_hits=30)[-1].path == "repo/TODO.md"


def test_plan_hits_round_robin_across_repos(tmp_path: Path) -> None:
    # One long TODO must not crowd the other repos out of the budget.
    long_repo = tmp_path / "long"
    short_repo = tmp_path / "short"
    long_repo.mkdir()
    short_repo.mkdir()
    (long_repo / "TODO.md").write_text("\n".join(f"- [ ] L{i}" for i in range(20)))
    (short_repo / "TODO.md").write_text("- [ ] S0\n- [ ] S1\n")
    config = RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[long_repo, short_repo],
        var_dir=tmp_path / "var",
    )
    hits = plan_hits(config, max_hits=6)
    repos = {hit.path.split("/")[0] for hit in hits if not hit.path.startswith("(")}
    assert repos == {"long", "short"}  # both represented despite the cap
    assert [h.text[-2:] for h in hits[:4]] == ["L0", "S0", "L1", "S1"]  # interleaved


def _repo_with_plan(tmp_path: Path, items: int = 40) -> RobinConfig:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "TODO.md").write_text("\n".join(f"- [ ] item {i}" for i in range(items)))
    return RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )


def _capture_sources(monkeypatch) -> None:
    """Stub the single LLM call site — compose() returns its sources verbatim."""
    monkeypatch.setattr(
        "robin.digest._compose_answer", lambda *args, **kwargs: ("digest text", 0.0)
    )


def test_daily_digest_carries_no_plan_items(tmp_path: Path, monkeypatch) -> None:
    # Plan files move on a weekly scale; re-sending them daily made consecutive daily
    # digests near-identical prose with no delta (report review 2026-07-26).
    _capture_sources(monkeypatch)
    config = _repo_with_plan(tmp_path)
    # The item-list label, not the word: the delta counter legitimately reports how
    # many items are open, it just does not enumerate them.
    _, sources, _ = compose(config, "daily", now=NOW)
    assert not any(hit.text.startswith("open plan item") for hit in sources)
    _, weekly_sources, _ = compose(config, "weekly", now=NOW)
    assert any(hit.text.startswith("open plan item") for hit in weekly_sources)


def test_daily_digest_carries_the_plan_delta(tmp_path: Path, monkeypatch) -> None:
    # The daily digest drops the full plan list but must still report movement —
    # that is what makes it a delta rather than a shorter copy of the weekly.
    _capture_sources(monkeypatch)
    config = _repo_with_plan(tmp_path, items=3)
    _, sources, _ = compose(config, "daily", now=NOW)
    movement = [hit for hit in sources if hit.path == "(plan-delta)"]
    assert len(movement) == 1
    assert "3 open plan item" in movement[0].text


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_both_digests_disclose_missing_plan_files(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    # Both cadences make claims about remaining work — the daily through the delta
    # counters, the weekly through the list — so both must say whose plans are absent.
    _capture_sources(monkeypatch)
    config = _repo_with_plan(tmp_path, items=2)  # vault has no plan file
    _, sources, _ = compose(config, kind, now=NOW)
    coverage = [hit for hit in sources if hit.path == "(plan-coverage)"]
    assert len(coverage) == 1 and "vault" in coverage[0].text


@pytest.mark.parametrize("kind", ["daily", "weekly"])
def test_both_digests_carry_the_labelling_gap(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    # Both cadences claim things about remaining work, so both should say how much of
    # it has nobody attached to it.
    _capture_sources(monkeypatch)
    config = _repo_with_plan(tmp_path, items=4)
    _, sources, _ = compose(config, kind, now=NOW)
    fields = [hit for hit in sources if hit.path == "(plan-fields)"]
    assert len(fields) == 1 and "4 of 4" in fields[0].text


def _digest_env(tmp_path: Path, monkeypatch) -> Path:
    """A workspace load_config() discovers, plus a stubbed LLM call site."""
    _capture_sources(monkeypatch)
    monkeypatch.setenv("ROBIN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("ROBIN_VAR_DIR", str(tmp_path / "var"))
    (tmp_path / "vault").mkdir(exist_ok=True)
    maestro = tmp_path / "Maestro"  # a name load_config() discovers as a mirror
    maestro.mkdir(exist_ok=True)
    (maestro / "TODO.md").write_text("- [ ] first item\n")
    return tmp_path / "var" / "plan-state-daily.json"


def _explode(*args, **kwargs):
    raise RuntimeError("channel down")


@pytest.mark.parametrize("failing_step", ["robin.digest.persist", "robin.digest.post"])
def test_run_advances_the_baseline_only_after_the_digest_reaches_the_team(
    tmp_path: Path, monkeypatch, failing_step: str
) -> None:
    # The snapshot is the digest's memory. Advancing it on a run that never reached the
    # team would silently swallow a window's movement — including a failed post(), not
    # just a failed persist() (Copilot review, PR #21).
    state = _digest_env(tmp_path, monkeypatch)
    monkeypatch.setattr(failing_step, _explode)
    with pytest.raises(RuntimeError):
        run("daily")
    assert not state.is_file()  # failed run leaves the baseline where it was


def test_run_records_the_baseline_after_a_clean_run(
    tmp_path: Path, monkeypatch
) -> None:
    state = _digest_env(tmp_path, monkeypatch)
    run("daily")
    assert "first item" in state.read_text()


def test_weekly_plan_budget_is_configurable(tmp_path: Path, monkeypatch) -> None:
    # The default budget must cover the ecosystem's real open-item count, and the
    # knob must be pinnable per deployment like every other runtime limit.
    _capture_sources(monkeypatch)
    config = _repo_with_plan(tmp_path, items=40)
    _, sources, _ = compose(config, "weekly", now=NOW)
    assert not any(hit.path == "(plan-items-truncated)" for hit in sources)
    tight = replace(config, plan_items_max=5)
    _, tight_sources, _ = compose(tight, "weekly", now=NOW)
    assert any(hit.path == "(plan-items-truncated)" for hit in tight_sources)


def test_weekly_digest_gets_a_larger_change_budget(tmp_path: Path, monkeypatch) -> None:
    # A week holds ~7x a day's commits (83 across the mirrors in the 07-20 window vs a
    # 60-hit budget): one flat budget makes the weekly digest permanently "PARTIAL".
    _capture_sources(monkeypatch)
    budgets: dict[str, int] = {}

    def record(config, period, *, max_hits):
        budgets[period.label] = max_hits
        return []

    monkeypatch.setattr("robin.digest.collect_changes", record)
    config = _repo_with_plan(tmp_path)
    compose(config, "daily", now=NOW)
    compose(config, "weekly", now=NOW)
    assert budgets["weekly digest window"] > budgets["daily digest window"]


def test_plan_truncation_marker_names_partial_repos(tmp_path: Path) -> None:
    # "30 of 62" alone leaves the reader unable to tell which repos are partial.
    long_repo = tmp_path / "long"
    short_repo = tmp_path / "short"
    long_repo.mkdir()
    short_repo.mkdir()
    (long_repo / "TODO.md").write_text("\n".join(f"- [ ] L{i}" for i in range(20)))
    (short_repo / "TODO.md").write_text("- [ ] S0\n")
    config = RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[long_repo, short_repo],
        var_dir=tmp_path / "var",
    )
    marker = plan_hits(config, max_hits=6)[-1]
    assert marker.path == "(plan-items-truncated)"
    assert "long" in marker.text  # the partial repo is named
    assert "short" not in marker.text  # fully covered repos are not flagged


def test_watched_repos_hit_lists_all_mirrors(tmp_path: Path) -> None:
    config = RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[tmp_path / "maestro", tmp_path / "arbiter"],
        var_dir=tmp_path / "var",
    )
    hit = watched_repos_hit(config)
    assert hit.path == "(watched-repos)"
    assert "vault, maestro, arbiter" in hit.text


def test_liveness_flags_missing_then_clears(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert set(stale_kinds(config, now=time.time())) == {"daily", "weekly"}
    persist(config, "daily", "x", now=NOW)
    persist(config, "weekly", "y", now=NOW)
    fresh = NOW.timestamp() + 3600
    assert stale_kinds(config, now=fresh) == []
    late = NOW.timestamp() + (24 + 6) * 3600 + 60  # past daily cadence + grace
    assert stale_kinds(config, now=late) == ["daily"]
