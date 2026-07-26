"""Env parsing for the runtime knobs (slot 17: secrets and settings come from env)."""

from __future__ import annotations

from pathlib import Path

import pytest

from robin.config import _ECOSYSTEM_REPOS, load_config


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


def test_mirror_list_matches_the_deploy_script() -> None:
    """The list Robin reads and the list the VPS clones must name the same repos.

    They are two hardcoded copies of one fact, and they drifted apart once already: the
    2026-07-16 renames landed in the docs but in neither list, so every checkout cloned
    under the canonical names lost maestro and libretto from the digest without a word.
    """
    setup = (Path(__file__).resolve().parents[1] / "deploy" / "setup.sh").read_text(
        encoding="utf-8"
    )
    line = next(ln for ln in setup.splitlines() if ln.startswith("REPOS=("))
    cloned = set(line[len("REPOS=(") : line.rindex(")")].split())
    # setup.sh also clones the knowledge repo, which is the vault rather than a mirror
    # of an ecosystem repo, and is therefore absent from _ECOSYSTEM_REPOS by design.
    assert cloned == set(_ECOSYSTEM_REPOS) | {"prograph-vault"}


def test_mirror_names_are_canonical() -> None:
    """Names renamed in 2026-07 must not creep back: a mirror directory is named by a
    plain `git clone`, so an old name resolves only where a stale clone still sits."""
    assert not {"Maestro", "open-prose"} & set(_ECOSYSTEM_REPOS)
