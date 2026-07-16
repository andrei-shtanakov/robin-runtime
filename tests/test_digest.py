"""Digest windowing/persistence (§6.4 read-back) and §7 liveness staleness."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from robin.config import RobinConfig
from robin.digest import latest, persist, plan_hits, watched_repos_hit, window
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
