"""Telegram gate/addressing logic — pure functions tested with attribute stubs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from robin.adapters.telegram import _addressed_text, _allowed, gate
from robin.config import RobinConfig


def _update(user_id: int = 42, username: str | None = "alice") -> SimpleNamespace:
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id, username=username))


def _config(tmp_path: Path, allowed: tuple[str, ...]) -> RobinConfig:
    return RobinConfig(
        vault_path=tmp_path, repo_paths=[], var_dir=tmp_path / "var",
        allowed_dm_users=allowed,
    )


def test_allowlist_matches_id_and_username(tmp_path: Path) -> None:
    config = _config(tmp_path, ("42",))
    assert _allowed(config, _update(42))
    assert not _allowed(config, _update(7))
    by_name = _config(tmp_path, ("@alice",))
    assert _allowed(by_name, _update(7, "alice"))
    assert not _allowed(by_name, _update(7, "bob"))


def test_empty_allowlist_is_open(tmp_path: Path) -> None:
    assert _allowed(_config(tmp_path, ()), _update(999, None))


def test_gate_refuses_stranger_with_text(tmp_path: Path) -> None:
    refusal = gate(_config(tmp_path, ("42",)), _update(7))
    assert refusal is not None and "maintainer" in refusal


def test_gate_passes_allowed_user(tmp_path: Path) -> None:
    assert gate(_config(tmp_path, ("42",)), _update(42)) is None


def _message(text: str, chat_type: str) -> SimpleNamespace:
    return SimpleNamespace(text=text, chat=SimpleNamespace(type=chat_type))


def test_dm_text_is_always_addressed() -> None:
    assert _addressed_text(_message("hello", "private"), "robin_bot") == "hello"


def test_group_needs_mention() -> None:
    assert _addressed_text(_message("hello", "supergroup"), "robin_bot") is None
    assert (
        _addressed_text(_message("@robin_bot what is arbiter?", "supergroup"), "robin_bot")
        == "what is arbiter?"
    )
    assert _addressed_text(_message("@robin_bot", "supergroup"), "robin_bot") is None
