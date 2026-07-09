"""M0 tool-layer tests: the read-only KB search finds the agents-catalog SSOT answer in the
mounted knowledge repo with a real citation, and never reads `_cowork_output/`. No API key."""

from __future__ import annotations

import pytest

from robin.config import load_config
from robin.kb import Kb


def test_search_finds_agents_catalog_ssot() -> None:
    config = load_config()
    if not config.vault_path.is_dir():
        pytest.skip("knowledge repo not mounted")
    kb = Kb(config.read_roots())
    hits = kb.search("agents-catalog.toml")
    assert any("agents-catalog.toml" in hit.text for hit in hits)
    assert any(
        "decisions" in hit.path and "eco-003" in hit.path.lower() for hit in hits
    ), "expected the agents-catalog ADR among the grounding hits"


def test_terms_tokenizes_cyrillic_and_keeps_acronyms() -> None:
    from robin.kb import _terms

    terms = _terms("Что можешь сказать о сегодняшних изменениях в проектах?")
    assert "изменениях" in terms and "проектах" in terms
    assert "что" not in terms and "можешь" not in terms  # RU stopwords dropped
    assert "kb" in _terms("есть что-то в KB?")  # short acronym survives


def test_grounding_matches_cyrillic_content(tmp_path) -> None:
    (tmp_path / "note.md").write_text("Дайджест: изменения в проектах экосистемы.\n")
    kb = Kb([tmp_path])
    hits = kb.grounding_hits("что нового в проектах?")
    assert hits and hits[0].path == "note.md"


def test_cowork_output_is_never_read() -> None:
    config = load_config()
    if not config.vault_path.is_dir():
        pytest.skip("knowledge repo not mounted")
    kb = Kb(config.read_roots())
    hits = kb.search("cowork")
    assert all("_cowork_output" not in hit.path for hit in hits)
