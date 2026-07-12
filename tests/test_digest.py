"""Digest windowing/persistence (§6.4 read-back) and §7 liveness staleness."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from robin.config import RobinConfig
from robin.digest import persist, window
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


def test_liveness_flags_missing_then_clears(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert set(stale_kinds(config, now=time.time())) == {"daily", "weekly"}
    persist(config, "daily", "x", now=NOW)
    persist(config, "weekly", "y", now=NOW)
    fresh = NOW.timestamp() + 3600
    assert stale_kinds(config, now=fresh) == []
    late = NOW.timestamp() + (24 + 6) * 3600 + 60  # past daily cadence + grace
    assert stale_kinds(config, now=late) == ["daily"]
