"""Epic-axis shadow (spec §7): classification, fail-closed sources, determinism."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from robin.changes import Period
from robin.config import RobinConfig
from robin.digest import run
from robin.epic_shadow import (
    collect,
    record_failure,
    render_json,
    render_md,
    run_shadow,
)

SINCE = datetime(2026, 7, 2, tzinfo=timezone.utc)
UNTIL = datetime(2026, 7, 9, tzinfo=timezone.utc)
PERIOD = Period(since=SINCE, until=UNTIL, label="weekly digest window")
IN_WINDOW = "2026-07-08T10:00:00+00:00"

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _commit(repo: Path, message: str, date: str = IN_WINDOW) -> None:
    env = {
        **os.environ,
        **_GIT_ENV,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
    }
    marker = repo / f"f{len(list(repo.glob('f*.txt')))}.txt"
    marker.write_text(message[:40])
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message], check=True, env=env
    )


def _registry_mirror(base: Path, body: str | bytes | None) -> Path:
    mirror = base / "ai-orchestrators-workspace"
    _init_repo(mirror)
    if isinstance(body, bytes):
        (mirror / "epics.toml").write_bytes(body)
    elif body is not None:
        (mirror / "epics.toml").write_text(body)
    return mirror


_REGISTRY = '[epics."eco.x"]\ntitle = "Ось X"\n[epics."eco.y"]\ntitle = "Y"\n'


def _config(tmp_path: Path, repos: list[Path], **kwargs) -> RobinConfig:
    vault = tmp_path / "vault"
    if not vault.exists():
        _init_repo(vault)
    return RobinConfig(
        vault_path=vault, repo_paths=repos, var_dir=tmp_path / "var", **kwargs
    )


def test_final_block_trailer_classifies_and_split_block_does_not(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "good\n\nEpic: eco.x\nCo-Authored-By: t <t@t>")
    # The blank line pushes Epic: out of the final trailer block — git does not
    # see a trailer, and neither must we (the fleet-wide 2026-08-26 defect).
    _commit(repo, "split\n\nEpic: eco.x\n\nCo-Authored-By: t <t@t>")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.per_epic["eco.x"]["commits"] == 1
    assert snapshot.buckets["unclassified"]["commits"] == 1
    assert snapshot.per_epic["eco.x"]["title"] == "Ось X"
    assert snapshot.per_epic["eco.x"]["repos"] == ["demo"]


def test_conflict_duplicates_and_empty_values(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "two\n\nEpic: eco.x\nEpic: eco.y")
    _commit(repo, "dup\n\nEpic: eco.x\nEpic: eco.x")
    _commit(repo, "blank\n\nEpic:\nCo-Authored-By: t <t@t>")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.buckets["conflict"]["commits"] == 1
    assert snapshot.buckets["conflict"]["examples"][0]["keys"] == ["eco.x", "eco.y"]
    assert snapshot.per_epic["eco.x"]["commits"] == 1  # duplicates collapse
    assert snapshot.buckets["unclassified"]["commits"] == 1  # empty value dropped
    # conflict example renders both raw values, JSON-quoted, unambiguously
    assert '"eco.x" + "eco.y"' in render_md(snapshot)


def test_unregistered_key_and_defect_counter(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "typo\n\nEpic: eco.nope")
    _commit(repo, "bug\n\nEpic: eco.x\nDefect: code")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.buckets["unregistered"]["commits"] == 1
    assert snapshot.buckets["unregistered"]["examples"][0]["keys"] == ["eco.nope"]
    assert snapshot.per_epic["eco.x"]["defects"] == 1


def test_merge_commit_lands_in_unclassified(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "base\n\nEpic: eco.x")
    env = {
        **os.environ,
        **_GIT_ENV,
        "GIT_AUTHOR_DATE": IN_WINDOW,
        "GIT_COMMITTER_DATE": IN_WINDOW,
    }
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-q", "-c", "feature"], check=True, env=env
    )
    _commit(repo, "work\n\nEpic: eco.y")
    subprocess.run(["git", "-C", str(repo), "switch", "-q", "-"], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(repo), "merge", "-q", "--no-ff", "-m", "merge", "feature"],
        check=True,
        env=env,
    )
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    # unlike changes.git_log (--no-merges) the merge commit itself is counted
    assert snapshot.buckets["unclassified"]["commits"] == 1
    assert snapshot.per_epic["eco.x"]["commits"] == 1
    assert snapshot.per_epic["eco.y"]["commits"] == 1


@pytest.mark.parametrize(
    "body",
    [
        None,  # file missing
        b"\xff\xfe\x00broken",  # decode error
        "epics = [not toml",  # parse error
        'x = "no epics section"',  # [epics] missing
        "epics = 3",  # [epics] not a table
    ],
)
def test_registry_failures_yield_unavailable_not_a_crash(tmp_path: Path, body) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "tagged\n\nEpic: eco.x")
    mirror = _registry_mirror(tmp_path, body)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.provenance["registry"].startswith("unavailable")
    assert snapshot.buckets["unverified"]["commits"] == 1
    assert "unregistered" not in snapshot.buckets  # nothing to verify against
    assert "registry: unavailable" in render_md(snapshot)


def test_registry_mirror_absent_is_unavailable(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "tagged\n\nEpic: eco.x")
    snapshot = collect(_config(tmp_path, [repo]), PERIOD)
    assert snapshot.provenance["registry"] == "unavailable: mirror not present"
    assert snapshot.buckets["unverified"]["commits"] == 1


def test_failing_git_and_missing_mirrors_are_disclosed(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    config = _config(tmp_path, [not_a_repo, mirror], missing_mirrors=("ghost",))
    snapshot = collect(config, PERIOD)
    assert snapshot.provenance["mirrors"]["plain"].startswith("skipped")
    assert snapshot.provenance["mirrors"]["ghost"] == "skipped: not a directory"
    assert snapshot.provenance["mirrors"]["vault"] in (
        "read",
        "skipped: git exited 128",
    )


def test_unparsed_record_marks_the_mirror_partial(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    mirror = _registry_mirror(tmp_path, _REGISTRY)

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if "log" in args and str(repo) in args:
            return SimpleNamespace(
                returncode=0, stdout="\x1eSHA1\x1feco.x\x1f\x1e" + "garbage-no-fields"
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("robin.epic_shadow.subprocess.run", fake_run)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.provenance["mirrors"]["demo"] == "partial: 1 unparsed"
    assert snapshot.per_epic["eco.x"]["commits"] == 1


def test_zero_window_still_writes_the_full_pair(tmp_path: Path) -> None:
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    config = _config(tmp_path, [mirror])
    json_path, md_path = run_shadow(config, PERIOD)
    payload = json.loads(json_path.read_text())
    assert payload["per_epic"] == {}
    md = md_path.read_text()
    for row in ("unclassified", "unregistered", "conflict"):
        assert payload["buckets"][row]["commits"] == 0
        assert f"- {row} — 0 коммитов" in md
    assert payload["provenance"]["mirrors"]


def test_generated_at_labels_match_and_equal_until(tmp_path: Path) -> None:
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    config = _config(tmp_path, [mirror])
    json_path, md_path = run_shadow(config, PERIOD)
    label = json.loads(json_path.read_text())["generated_at"]
    assert label == UNTIL.isoformat()
    assert f"generated_at: {label}" in md_path.read_text()


def test_shadow_requires_a_pinned_until(tmp_path: Path) -> None:
    config = _config(tmp_path, [])
    with pytest.raises(ValueError):
        collect(config, Period(since=SINCE, until=None, label="open"))


def test_commit_after_until_is_excluded(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "inside\n\nEpic: eco.x")
    _commit(repo, "late\n\nEpic: eco.x", date="2026-07-10T10:00:00+00:00")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    assert snapshot.per_epic["eco.x"]["commits"] == 1  # the late one is not counted


def test_registry_unreadable_file_is_unavailable(tmp_path: Path) -> None:
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "tagged\n\nEpic: eco.x")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    (mirror / "epics.toml").chmod(0o000)
    try:
        snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    finally:
        (mirror / "epics.toml").chmod(0o644)
    assert snapshot.provenance["registry"].startswith("unavailable: PermissionError")
    assert snapshot.buckets["unverified"]["commits"] == 1


def test_examples_are_canonically_sorted_regardless_of_git_order(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    mirror = _registry_mirror(tmp_path, _REGISTRY)

    real_run = subprocess.run

    def fake_run(args, **kwargs):
        if "log" in args and str(repo) in args:
            # git hands records newest-first: bbb before aaa
            return SimpleNamespace(
                returncode=0,
                stdout="\x1ebbb\x1feco.zzz\x1f\x1eaaa\x1feco.nope\x1f",
            )
        return real_run(args, **kwargs)

    monkeypatch.setattr("robin.epic_shadow.subprocess.run", fake_run)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    shas = [e["sha"] for e in snapshot.buckets["unregistered"]["examples"]]
    assert shas == ["aaa", "bbb"]  # sorted by (repo, sha), not by git order


def test_same_input_renders_byte_identical_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "one\n\nEpic: eco.x")
    _commit(repo, "typo\n\nEpic: eco.nope")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    config = _config(tmp_path, [repo, mirror])
    first = collect(config, PERIOD)
    second = collect(config, PERIOD)
    assert render_json(first) == render_json(second)
    assert render_md(first) == render_md(second)


def test_md_and_json_agree_on_counters(tmp_path: Path) -> None:
    repo = tmp_path / "demo"
    _init_repo(repo)
    _commit(repo, "one\n\nEpic: eco.x")
    mirror = _registry_mirror(tmp_path, _REGISTRY)
    snapshot = collect(_config(tmp_path, [repo, mirror]), PERIOD)
    payload = json.loads(render_json(snapshot))
    md = render_md(snapshot)
    assert f"- eco.x «Ось X» — {payload['per_epic']['eco.x']['commits']} коммитов" in md


def test_record_failure_survives_an_unwritable_var(tmp_path: Path) -> None:
    var = tmp_path / "var"
    var.write_text("a file where a directory should be")
    config = RobinConfig(vault_path=tmp_path, repo_paths=[], var_dir=var)
    record_failure(config, "boom")  # must not raise (spec §3.1)


def _weekly_env(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(
        "robin.digest._compose_answer",
        lambda question, sources, config, rules=None: ("digest text", None),
    )
    monkeypatch.setenv("ROBIN_VAULT", str(tmp_path / "vault"))
    monkeypatch.setenv("ROBIN_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("ROBIN_FRESHNESS_REPO", "")
    (tmp_path / "vault").mkdir(exist_ok=True)
    return tmp_path / "var"


def test_run_weekly_isolates_a_shadow_failure(tmp_path: Path, monkeypatch) -> None:
    var = _weekly_env(tmp_path, monkeypatch)

    def explode(config, period):
        raise RuntimeError("shadow broke")

    monkeypatch.setattr("robin.epic_shadow.run_shadow", explode)
    run("weekly")  # must not raise
    assert (var / "digests").is_dir()  # the digest itself landed
    records = [
        json.loads(line)
        for line in (var / "interactions.jsonl").read_text().splitlines()
    ]
    failure = [r for r in records if r["surface"] == "epic-shadow"]
    assert failure and failure[0]["ok"] is False


def test_run_daily_does_not_start_the_shadow(tmp_path: Path, monkeypatch) -> None:
    var = _weekly_env(tmp_path, monkeypatch)
    run("daily")
    assert not (var / "epic-shadow").exists()


def test_run_hands_the_same_period_to_compose_and_shadow(
    tmp_path: Path, monkeypatch
) -> None:
    var = _weekly_env(tmp_path, monkeypatch)
    seen: dict[str, Period] = {}

    from robin import digest as digest_module

    real_compose = digest_module.compose

    def spy_compose(config, kind, **kwargs):
        seen["compose"] = kwargs["period"]
        return real_compose(config, kind, **kwargs)

    def spy_shadow(config, period):
        seen["shadow"] = period
        return (var / "a", var / "b")

    from robin.plan_state import delta_hit as real_delta_hit

    def spy_collect_changes(config, period, **kwargs):
        seen["collect_changes"] = period
        return []

    def spy_delta_hit(config, kind, *, now=None):
        seen["delta_now"] = now
        return real_delta_hit(config, kind, now=now)

    fixed_now = datetime(2026, 7, 9, 9, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("robin.digest.compose", spy_compose)
    monkeypatch.setattr("robin.digest.collect_changes", spy_collect_changes)
    monkeypatch.setattr("robin.digest.delta_hit", spy_delta_hit)
    monkeypatch.setattr("robin.epic_shadow.run_shadow", spy_shadow)
    run("weekly", now=fixed_now)
    assert seen["compose"] is seen["shadow"]  # the same object, not an equal one
    # ...and the same object reaches the digest's own collector
    assert seen["collect_changes"] is seen["compose"]
    assert seen["compose"].until == fixed_now  # the top is pinned to the run's now
    # the run's single now reaches the time-dependent sources too — without it
    # delta_hit falls back to its own wall clock (review finding, round 1)
    assert seen["delta_now"] == fixed_now
