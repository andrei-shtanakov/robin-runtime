"""Digest windowing/persistence (§6.4 read-back) and §7 liveness staleness."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from robin.config import RobinConfig
from robin.digest import latest, persist, plan_hits, window
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
    assert len(hits) == 3
    assert all(text.startswith("open plan item: ") for text in texts)
    assert not any("shipped thing" in text for text in texts)
    assert not any("free-form note" in text for text in texts)
    assert hits[0].path == "maestro/TODO.md" and hits[0].line == 3


def test_plan_hits_caps_and_degrades_without_plans(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert plan_hits(config) == []  # no plan files anywhere — section grounding empty
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "TODO.md").write_text("\n".join("- [ ] item" for _ in range(30)))
    config = RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )
    assert len(plan_hits(config, max_hits=5)) == 5


def test_liveness_flags_missing_then_clears(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert set(stale_kinds(config, now=time.time())) == {"daily", "weekly"}
    persist(config, "daily", "x", now=NOW)
    persist(config, "weekly", "y", now=NOW)
    fresh = NOW.timestamp() + 3600
    assert stale_kinds(config, now=fresh) == []
    late = NOW.timestamp() + (24 + 6) * 3600 + 60  # past daily cadence + grace
    assert stale_kinds(config, now=late) == ["daily"]
