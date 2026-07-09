"""Voice helpers: speakable() text preparation and provider registry errors."""

from __future__ import annotations

import pytest

from robin.config import RobinConfig
from robin.voice import make_stt, make_tts, speakable


def test_speakable_strips_cites_and_markup() -> None:
    text = (
        "The arbiter repo is the policy engine (`arbiter/README.md:3`).\n\n"
        "**Details** follow.\n"
        "• `authored/decisions/adr.md:22`\n"
        "- another bullet source\n"
    )
    spoken = speakable(text)
    assert "arbiter/README.md" not in spoken
    assert "•" not in spoken and "**" not in spoken
    assert "policy engine" in spoken


def test_speakable_caps_length_at_sentence() -> None:
    text = "One sentence here. " * 200
    spoken = speakable(text, max_chars=100)
    assert len(spoken) <= 100
    assert spoken.endswith(".")


def test_unknown_provider_is_loud(tmp_path) -> None:
    config = RobinConfig(
        vault_path=tmp_path, repo_paths=[], stt_provider="nope", tts_provider="nope"
    )
    with pytest.raises(ValueError):
        make_stt(config)
    with pytest.raises(ValueError):
        make_tts(config)
