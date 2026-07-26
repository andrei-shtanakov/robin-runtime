"""Env parsing for the runtime knobs (slot 17: secrets and settings come from env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robin.config import load_config


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("ROBIN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("ROBIN_VAR_DIR", str(tmp_path / "var"))


def test_plan_exempt_defaults_to_empty() -> None:
    assert load_config().plan_exempt == ()


def test_plan_exempt_parses_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ROBIN_PLAN_EXEMPT", " prograph-vault , discovery ,, ")
    assert load_config().plan_exempt == ("prograph-vault", "discovery")
