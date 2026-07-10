"""Stage-2 failure log: detectors and the append-only gaps.jsonl store. No API key."""

from __future__ import annotations

from pathlib import Path

import pytest

from robin import gaps, memory
from robin.agent import ask
from robin.config import RobinConfig
from robin.kb import Hit


def _config(tmp_path: Path) -> RobinConfig:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return RobinConfig(vault_path=vault, repo_paths=[], var_dir=tmp_path / "var")


# --- detectors ---------------------------------------------------------------


def test_zero_retrieval_counts_only_positive_evidence() -> None:
    assert gaps.is_zero_retrieval([])
    marker = Hit("(no-changes-found)", 1, "No commits found …")
    assert gaps.is_zero_retrieval([marker])  # маркеры — не источники
    real = Hit("authored/decisions/adr.md", 3, "the catalog lives in atp-platform")
    assert not gaps.is_zero_retrieval([marker, real])


def test_reformulation_within_window_with_overlap() -> None:
    assert gaps.is_reformulation(
        "какие сегодняшние изменения в проекте?",
        1000.0,
        "что изменилось сегодня в проектах?",
        now=1000.0 + 60,
    )


def test_reformulation_outside_window_is_not_flagged() -> None:
    assert not gaps.is_reformulation(
        "какие изменения в проекте?",
        1000.0,
        "какие изменения в проекте?",
        now=1000.0 + gaps.REFORMULATION_WINDOW_S + 1,
    )


def test_topic_change_is_not_a_reformulation() -> None:
    assert not gaps.is_reformulation(
        "какие изменения в проекте?",
        1000.0,
        "кто владеет agents-catalog?",
        now=1000.0 + 60,
    )


# --- store -------------------------------------------------------------------


def test_log_gap_appends_and_truncates(tmp_path: Path) -> None:
    config = _config(tmp_path)
    gaps.log_gap(
        config,
        surface="telegram",
        chat="123",
        requester="42",
        question="q" * 900,
        fail_signal="gap_command",
        comment="c" * 900,
    )
    gaps.log_gap(config, surface="cli", question=None, fail_signal="zero_retrieval")
    records = gaps.read_gaps(config)
    assert len(records) == 2  # append-only
    assert len(records[0]["question"]) == 500 and len(records[0]["comment"]) == 500
    assert records[1]["fail_signal"] == "zero_retrieval"


def test_unknown_signal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        gaps.log_gap(_config(tmp_path), surface="cli", question="q", fail_signal="oops")


# --- integration: ask() logs zero retrieval ----------------------------------


def test_ask_logs_zero_retrieval_with_answer_class(tmp_path: Path) -> None:
    config = _config(tmp_path)  # пустой vault: выдача гарантированно нулевая
    ask("what is the meaning of maestro?", config, chat="c1", retrieve_only=True)
    records = gaps.read_gaps(config)
    assert len(records) == 1
    record = records[0]
    assert record["fail_signal"] == "zero_retrieval"
    assert record["answer_class"] == "kb"
    assert record["chat"] == "c1"


def test_ask_classifies_temporal_zero_retrieval(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ask("что изменилось сегодня?", config, retrieve_only=True)
    records = gaps.read_gaps(config)
    assert records and records[-1]["answer_class"] == "temporal"


def test_ask_does_not_log_when_evidence_exists(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.vault_path / "note.md").write_text("maestro is the orchestrator\n")
    ask("what is maestro?", config, retrieve_only=True)
    assert gaps.read_gaps(config) == []


# --- memory helper ------------------------------------------------------------


def test_last_user_turn_returns_text_and_ts(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert memory.last_user_turn(config, "telegram", "1") is None
    memory.append(config, "telegram", "1", "user", "first question")
    memory.append(config, "telegram", "1", "robin", "answer")
    memory.append(config, "telegram", "1", "user", "second question")
    turn = memory.last_user_turn(config, "telegram", "1")
    assert turn is not None
    text, ts = turn
    assert text == "second question" and isinstance(ts, int)
