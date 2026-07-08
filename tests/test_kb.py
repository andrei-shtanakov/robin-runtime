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


def test_cowork_output_is_never_read() -> None:
    config = load_config()
    if not config.vault_path.is_dir():
        pytest.skip("knowledge repo not mounted")
    kb = Kb(config.read_roots())
    hits = kb.search("cowork")
    assert all("_cowork_output" not in hit.path for hit in hits)
