"""M1 grounding tests: authoritative sources rank first and Robin's own scaffolding is excluded
(finding #8); prompt assembly is correct. No API key required."""

from __future__ import annotations

import pytest

from robin.agent import build_prompt
from robin.config import load_config
from robin.kb import Hit, Kb


def test_grounding_ranks_authoritative_and_excludes_scaffolding() -> None:
    config = load_config()
    if not config.vault_path.is_dir():
        pytest.skip("knowledge repo not mounted")
    kb = Kb(config.read_roots())
    # a natural-language question must retrieve (term-based), not return 0 (regression guard)
    hits = kb.grounding_hits("Which repo owns the agents-catalog SSOT?")
    assert hits, "expected grounding hits for the NL question"
    assert hits[0].path.startswith("authored/decisions/"), (
        f"top source not authoritative: {hits[0].path}"
    )
    for hit in hits:
        assert "KICKOFF.md" not in hit.path
        assert "ROBIN-SPEC.local.md" not in hit.path
        assert not hit.path.startswith("robin/")


def test_build_prompt_includes_sources_and_question() -> None:
    sources = [Hit("authored/decisions/adr.md", 22, "atp-platform/method/agents-catalog.toml")]
    prompt = build_prompt("who owns the catalog?", sources)
    assert "authored/decisions/adr.md:22" in prompt
    assert "who owns the catalog?" in prompt
