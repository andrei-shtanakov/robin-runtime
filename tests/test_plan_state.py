"""Item-level plan movement between digest runs (the delta half of the digest)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robin.config import RobinConfig
from robin.plan_state import delta_hit, open_items, record

NOW = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path, todo: str) -> RobinConfig:
    repo = tmp_path / "maestro"
    repo.mkdir(exist_ok=True)
    (repo / "TODO.md").write_text(todo)
    return RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )


def test_open_items_survive_line_moves(tmp_path: Path) -> None:
    # Identity must be the item, not its position: inserting a line above must not
    # read as "one closed, one opened".
    config = _config(tmp_path, "- [ ] ship the gate\n")
    before = open_items(config)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "## New section\n- [x] unrelated done thing\n- [ ] ship the gate\n"
    )
    after = open_items(config)
    assert [item.key for item in before] == [item.key for item in after]
    assert after[0].line == 3  # the line did move


def test_item_text_drops_the_checkbox_marker(tmp_path: Path) -> None:
    # The marker is syntax, not content: it reads as noise once the item is quoted
    # inside a sentence about movement.
    config = _config(tmp_path, "* [ ]   ship the gate\n")
    assert open_items(config)[0].text == "ship the gate"


def test_first_run_reports_no_baseline_rather_than_no_movement(tmp_path: Path) -> None:
    # Negative-evidence rule: an absent snapshot is not proof that nothing moved.
    config = _config(tmp_path, "- [ ] alpha\n- [ ] beta\n")
    hit = delta_hit(config, "daily", now=NOW)
    assert hit is not None and hit.path == "(plan-delta)"
    assert "no previous snapshot" in hit.text
    assert "2 open plan item" in hit.text


def test_delta_names_newly_opened_and_closed_items(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n- [ ] beta\n")
    record(config, "daily", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [x] alpha\n- [ ] beta\n- [ ] gamma\n"
    )
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "1 newly opened" in hit.text and "gamma" in hit.text
    assert "1 closed" in hit.text and "alpha" in hit.text
    assert "2 open" in hit.text  # the new total, so coverage movement is visible


def test_unchanged_window_is_reported_as_no_movement(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "no plan items opened or closed" in hit.text


def test_snapshots_are_per_cadence(tmp_path: Path) -> None:
    # A daily run must not consume the weekly baseline, or the weekly digest would
    # report a day's movement as the week's.
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "weekly", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text("- [ ] alpha\n- [ ] beta\n")
    record(config, "daily", now=NOW + timedelta(days=1))
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [ ] alpha\n- [ ] beta\n- [ ] gamma\n"
    )
    daily = delta_hit(config, "daily", now=NOW + timedelta(days=2))
    weekly = delta_hit(config, "weekly", now=NOW + timedelta(days=2))
    assert "1 newly opened" in daily.text  # gamma only
    assert "2 newly opened" in weekly.text  # beta and gamma


def test_long_open_items_are_aged_from_first_sight(tmp_path: Path) -> None:
    # "Stale decisions" without a first-seen timestamp is guesswork; with one it is
    # a fact about how long the item has been sitting open.
    config = _config(tmp_path, "- [ ] ancient decision\n")
    record(config, "weekly", now=NOW)
    later = NOW + timedelta(days=45)
    record(config, "weekly", now=later)
    hit = delta_hit(config, "weekly", now=later)
    assert "open for 45 days" in hit.text and "ancient decision" in hit.text


def test_corrupt_snapshot_degrades_to_first_run(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    (config.var_dir / "plan-state-daily.json").write_text("{not json")
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "no previous snapshot" in hit.text  # never a false "nothing moved"


def test_record_carries_first_seen_across_runs(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    record(config, "daily", now=NOW + timedelta(days=3))
    state = json.loads((config.var_dir / "plan-state-daily.json").read_text())
    item = next(iter(state["items"].values()))
    assert item["first_seen"] == int(NOW.timestamp())  # age is not reset by re-sighting
