"""Stage-3 weekly self-review: window, clustering, work-order rendering, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from robin import gaps, selfreview
from robin.config import RobinConfig

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)

# log_gap stamps real wall-clock time; a window anchored at a FIXED past date
# silently empties once the calendar moves past it (this bit on 2026-07-12).
LIVE_NOW = datetime.now(timezone.utc) + timedelta(hours=1)


def _config(tmp_path: Path) -> RobinConfig:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return RobinConfig(vault_path=vault, repo_paths=[], var_dir=tmp_path / "var")


def _gap(
    ts: int, signal: str, answer_class: str | None = None, question: str = "q"
) -> dict:
    return {
        "ts": ts,
        "fail_signal": signal,
        "answer_class": answer_class,
        "question": question,
    }


def test_window_falls_back_to_seven_days(tmp_path: Path) -> None:
    since_ts, until_ts = selfreview.window(_config(tmp_path), now=NOW)
    assert until_ts - since_ts == 7 * 24 * 3600


def test_window_reads_marker_after_persist(tmp_path: Path) -> None:
    config = _config(tmp_path)
    selfreview.persist(config, "report", now=NOW)
    since_ts, _ = selfreview.window(config, now=NOW)
    assert since_ts == int(NOW.timestamp())  # следующее окно — от прошлого запуска


def test_in_window_filters_by_ts() -> None:
    records = [
        _gap(100, "gap_command"),
        _gap(200, "gap_command"),
        _gap(300, "gap_command"),
    ]
    assert [r["ts"] for r in selfreview.in_window(records, 150, 300)] == [200]


def test_cluster_groups_and_suggests() -> None:
    records = [
        _gap(1, "zero_retrieval", "temporal", "что изменилось вчера?"),
        _gap(2, "zero_retrieval", "temporal", "сегодняшние изменения?"),
        _gap(3, "zero_retrieval", "kb", "кто владеет X?"),
        _gap(4, "reformulation", None, "какие изменения в проекте?"),
    ]
    clusters = selfreview.cluster(records)
    assert [c.count for c in clusters] == [2, 1, 1]  # по убыванию
    temporal = clusters[0]
    assert (
        temporal.fail_signal == "zero_retrieval" and temporal.answer_class == "temporal"
    )
    assert "spec-runner" in temporal.suggestion  # tooling gap
    kb = next(c for c in clusters if c.answer_class == "kb")
    assert "prograph-vault" in kb.suggestion  # KB-gap PR candidate
    reform = next(c for c in clusters if c.fail_signal == "reformulation")
    assert "rewrite" in reform.suggestion


def test_cluster_caps_examples_and_dedupes() -> None:
    records = [_gap(i, "gap_command", None, f"q{i % 2}") for i in range(10)]
    (entry,) = selfreview.cluster(records)
    assert entry.count == 10
    assert entry.examples == ["q0", "q1"]  # дедуп + кап


def test_render_is_a_work_order() -> None:
    clusters = selfreview.cluster([_gap(1, "zero_retrieval", "kb", "кто владеет X?")])
    text = selfreview.render(clusters, total=1, since_ts=0, until_ts=86400, tz="UTC")
    assert "Провалов за окно: **1**" in text
    assert "Предлагаемый артефакт:" in text
    assert "кто владеет X?" in text
    assert "не меняет себя сам" in text  # инвариант duty


def test_render_empty_week_is_negative_evidence() -> None:
    text = selfreview.render([], total=0, since_ts=0, until_ts=86400, tz="UTC")
    assert "лог пуст" in text and "не «провалов не было»" in text


def test_persist_read_back_and_marker(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = selfreview.persist(config, "# report", now=NOW)
    assert path.read_text() == "# report"
    assert (config.var_dir / "selfreview" / "last.txt").is_file()


def test_end_to_end_from_gaps_log(tmp_path: Path) -> None:
    config = _config(tmp_path)
    gaps.log_gap(
        config,
        surface="telegram",
        chat="1",
        question="что изменилось вчера?",
        fail_signal="zero_retrieval",
        answer_class="temporal",
    )
    since_ts, until_ts = selfreview.window(config, now=LIVE_NOW)
    records = selfreview.in_window(gaps.read_gaps(config), since_ts, until_ts)
    clusters = selfreview.cluster(records)
    text = selfreview.render(
        clusters, total=len(records), since_ts=since_ts, until_ts=until_ts, tz=config.tz
    )
    assert "zero_retrieval × temporal — 1 шт." in text
