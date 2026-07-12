"""Conversation window (slots 12-14): rolling N turns, per-chat isolation."""

from __future__ import annotations

from pathlib import Path

from robin.config import RobinConfig
from robin.memory import append, recent


def _config(tmp_path: Path) -> RobinConfig:
    return RobinConfig(
        vault_path=tmp_path, repo_paths=[], var_dir=tmp_path / "var", history_turns=4
    )


def test_window_keeps_last_n(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for index in range(6):
        append(config, "telegram", "123", "user", f"q{index}")
    turns = recent(config, "telegram", "123")
    assert [t.text for t in turns] == ["q2", "q3", "q4", "q5"]


def test_chats_are_isolated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    append(config, "telegram", "123", "user", "hello")
    append(config, "web", "123", "user", "other surface")
    assert [t.text for t in recent(config, "telegram", "123")] == ["hello"]
    assert [t.text for t in recent(config, "web", "123")] == ["other surface"]
    assert recent(config, "telegram", "999") == []


def test_long_turns_are_truncated(tmp_path: Path) -> None:
    config = _config(tmp_path)
    append(config, "web", "a", "robin", "x" * 2000)
    (turn,) = recent(config, "web", "a")
    assert len(turn.text) == 500


def test_hostile_chat_id_stays_in_var(tmp_path: Path) -> None:
    config = _config(tmp_path)
    append(config, "web", "../../etc/passwd", "user", "hi")
    files = list((tmp_path / "var" / "chats").iterdir())
    assert len(files) == 1
    assert ".." not in files[0].name and "/" not in files[0].name.replace(
        files[0].name, ""
    )
