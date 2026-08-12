"""Env parsing for the runtime knobs (slot 17: secrets and settings come from env)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from robin.config import _ECOSYSTEM_REPOS, _PLAN_EXEMPT, load_config


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "vault").mkdir()
    monkeypatch.setenv("ROBIN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("ROBIN_VAR_DIR", str(tmp_path / "var"))


def test_plan_exempt_is_the_canonical_in_repo_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PP-101 stage 6 (owner ruling 2026-08-12): the registry lives in code
    # (_PLAN_EXEMPT, empty today) so two installations cannot disagree about
    # governance coverage; a stale ROBIN_PLAN_EXEMPT in someone's .env is
    # ignored rather than silently forking the canon.
    assert load_config().plan_exempt == _PLAN_EXEMPT == ()
    monkeypatch.setenv("ROBIN_PLAN_EXEMPT", "prograph-vault")
    assert load_config().plan_exempt == ()


def test_unresolved_mirror_names_are_reported_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # load_config used to filter unresolved names via .is_dir() silently; they
    # are now carried on the config so coverage can disclose them (PP-101).
    vault = tmp_path / "prograph-vault"
    vault.mkdir()
    (tmp_path / "maestro").mkdir()
    monkeypatch.setenv("ROBIN_VAULT", str(vault))
    config = load_config()
    assert [p.name for p in config.repo_paths] == ["maestro"]
    assert set(config.missing_mirrors) == set(_ECOSYSTEM_REPOS) - {"maestro"}


def test_freshness_reader_is_on_by_default_and_env_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Default-on: an opt-in watchdog that nobody enables watches nothing (issue #42).
    assert load_config().freshness_repo == "andrei-shtanakov/steward"
    monkeypatch.setenv("ROBIN_FRESHNESS_REPO", " ")
    assert load_config().freshness_repo == ""
    monkeypatch.setenv("ROBIN_FRESHNESS_REPO", "org/other-watch")
    assert load_config().freshness_repo == "org/other-watch"


def test_mirror_list_matches_the_deploy_script() -> None:
    """The list Robin reads and the list the VPS clones must name the same repos.

    They are two hardcoded copies of one fact, and they drifted apart once already: the
    2026-07-16 renames landed in the docs but in neither list, so every checkout cloned
    under the canonical names lost maestro and libretto from the digest without a word.
    """
    setup = (Path(__file__).resolve().parents[1] / "deploy" / "setup.sh").read_text(
        encoding="utf-8"
    )
    # Tolerant of shell cosmetics (indentation, spacing): this test exists to catch a
    # drifting repo set, and failing on a reformatted line would only teach the reader
    # to distrust it. A missing declaration is reported as such, not as StopIteration.
    match = re.search(r"^\s*REPOS=\(([^)]*)\)", setup, re.MULTILINE)
    assert match, "deploy/setup.sh no longer declares REPOS=(...)"
    cloned = set(match.group(1).split())
    # setup.sh also clones the knowledge repo, which is the vault rather than a mirror
    # of an ecosystem repo, and is therefore absent from _ECOSYSTEM_REPOS by design.
    assert cloned == set(_ECOSYSTEM_REPOS) | {"prograph-vault"}


def test_mirror_names_are_canonical() -> None:
    """Names renamed in 2026-07 must not creep back: a mirror directory is named by a
    plain `git clone`, so an old name resolves only where a stale clone still sits."""
    assert not {"Maestro", "open-prose"} & set(_ECOSYSTEM_REPOS)
