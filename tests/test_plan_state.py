"""Item-level plan movement between digest runs (the delta half of the digest)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import robin.plan_state as plan_state

from robin.config import RobinConfig
from robin.digest import plan_hits
from robin.plan_state import (
    coverage_hit,
    delta_hit,
    fields_hit,
    open_items,
    record,
)

NOW = datetime(2026, 7, 26, 9, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path, todo: str) -> RobinConfig:
    repo = tmp_path / "maestro"
    repo.mkdir(exist_ok=True)
    (repo / "TODO.md").write_text(todo)
    return RobinConfig(
        vault_path=tmp_path / "vault", repo_paths=[repo], var_dir=tmp_path / "var"
    )


def test_open_items_survive_line_moves(tmp_path: Path) -> None:
    # Identity must be the item, not its position: inserting a line above must not
    # read as "one closed, one opened".
    config = _config(tmp_path, "- [ ] ship the gate\n")
    before = open_items(config)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "## New section\n- [x] unrelated done thing\n- [ ] ship the gate\n"
    )
    after = open_items(config)
    assert [item.key for item in before] == [item.key for item in after]
    assert after[0].line == 3  # the line did move


def test_coverage_names_mirrors_without_a_plan_file(tmp_path: Path) -> None:
    # A repo with no plan file contributes nothing and used to do so silently, so the
    # "what remains" picture looked ecosystem-wide when it covered a fraction of it
    # (5 of 12 mirrors on 2026-07-26, and 56 of 62 items came from two files).
    config = _config(tmp_path, "- [ ] alpha\n")
    silent = tmp_path / "deployer"
    silent.mkdir()
    config = RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[tmp_path / "maestro", silent],
        var_dir=tmp_path / "var",
    )
    hit = coverage_hit(config)
    assert hit is not None and hit.path == "(plan-coverage)"
    assert "1/3 required mirrors" in hit.text
    assert "deployer" in hit.text and "vault" in hit.text
    assert "maestro" not in hit.text  # the covered repo is not in the gap list


def test_coverage_is_silent_when_every_mirror_has_a_plan(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").write_text("- [ ] vault work\n")
    assert coverage_hit(config) is None


def test_a_directory_named_like_a_plan_file_is_not_coverage(tmp_path: Path) -> None:
    # open_items() reads plan files and skips anything unreadable, so coverage must
    # apply the same test or the two disagree about the same repo (Copilot, PR #22).
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").mkdir()
    hit = coverage_hit(config)
    assert hit is not None and "vault" in hit.text


def test_an_unreadable_plan_file_is_not_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A file that exists but cannot be read yields no items either, so counting it as
    # coverage reopens the exact hole this marker closes (Copilot, PR #23).
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    unreadable = tmp_path / "vault" / "TODO.md"
    unreadable.write_text("- [ ] vault work\n")
    original = Path.read_text

    def deny(self, *args, **kwargs):
        if self == unreadable:
            raise PermissionError(f"denied: {self}")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", deny)
    assert open_items(config) and all(i.repo != "vault" for i in open_items(config))
    hit = coverage_hit(config)
    assert hit is not None and "vault" in hit.text


def test_exempt_mirrors_leave_the_coverage_expectation(tmp_path: Path) -> None:
    # A knowledge base is not a project with work (prograph-vault, owner ruling
    # 2026-07-26): reporting it as a gap every run turns an honesty marker into noise.
    config = _config(tmp_path, "- [ ] alpha\n")
    vault = tmp_path / "vault"
    vault.mkdir()
    exempt = replace(config, plan_exempt=("vault",))
    assert coverage_hit(config) is not None  # without the exemption it is a gap
    hit = coverage_hit(exempt)
    # Every mirror that is expected to have a plan does — but the applied
    # exemption is still disclosed instead of vanishing with the gap list
    # (PP-101 stage 6: permanent disclosure).
    assert hit is not None
    assert "1/1 required mirrors" in hit.text
    assert "no plan file" not in hit.text
    assert "not counted: vault" in hit.text
    assert "remains included" in hit.text


def test_exemptions_are_disclosed_not_silent(tmp_path: Path) -> None:
    # Shrinking the denominator without saying so is the silent-partial failure this
    # whole marker exists to prevent.
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "deployer").mkdir()
    config = replace(
        config,
        repo_paths=[tmp_path / "maestro", tmp_path / "deployer"],
        plan_exempt=("vault",),
    )
    hit = coverage_hit(config)
    assert hit is not None
    assert "1/2 required mirrors" in hit.text  # the exempt mirror left the denominator
    assert "deployer" in hit.text
    # ...and it is said out loud rather than dropping out of the count in silence
    assert "vault" in hit.text and "exempt" in hit.text.lower()


def test_an_exemption_matching_no_mirror_is_reported_as_ignored(tmp_path: Path) -> None:
    # A typo used to be echoed as "exempt, not counted" while the real repo stayed in
    # the gap list — the honesty marker itself lying (Copilot, PR #24).
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    config = replace(config, plan_exempt=("vualt",))
    hit = coverage_hit(config)
    assert hit is not None
    assert "vault" in hit.text  # the real mirror is still counted as a gap
    assert "1/2 required mirrors" in hit.text  # ...and still in the denominator
    assert "vualt" in hit.text and "ignored" in hit.text.lower()
    assert "not counted: vualt" not in hit.text  # never claimed as applied


def test_exemption_does_not_hide_items_a_repo_does_have(tmp_path: Path) -> None:
    # Exempt means "not expected to keep a plan", not "ignore its plan": if the repo
    # starts keeping one, its items must still reach the digest.
    config = _config(tmp_path, "- [ ] alpha\n")
    config = replace(config, plan_exempt=("maestro",))
    assert [item.text for item in open_items(config)] == ["alpha"]


def test_full_coverage_with_no_exemptions_stays_silent(tmp_path: Path) -> None:
    # None must keep meaning "genuinely nothing to disclose", or the marker
    # becomes daily noise instead of an honesty signal.
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").write_text("- [ ] vault work\n")
    assert coverage_hit(config) is None


def test_unknown_exemption_is_disclosed_even_at_full_coverage(
    tmp_path: Path,
) -> None:
    # A typo in the registry used to be visible only while something else was
    # missing — the misconfiguration hid exactly when everything looked fine.
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").write_text("- [ ] vault work\n")
    config = replace(config, plan_exempt=("vualt",))
    hit = coverage_hit(config)
    assert hit is not None
    assert "2/2 required mirrors" in hit.text
    assert "vualt" in hit.text and "ignored" in hit.text.lower()


def test_unresolved_mirror_names_are_disclosed(tmp_path: Path) -> None:
    # A list entry that stopped resolving used to vanish from the digest
    # silently (maestro/libretto, 2026-07). PP-101 stage 6: fail loud.
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").write_text("- [ ] vault work\n")
    config = replace(config, missing_mirrors=("impresario", "deployer"))
    hit = coverage_hit(config)
    assert hit is not None
    assert "not resolved on disk" in hit.text
    assert "impresario" in hit.text and "deployer" in hit.text
    assert "absent from this digest entirely" in hit.text


def test_everything_exempt_reads_as_an_alarm_not_zero_over_zero(
    tmp_path: Path,
) -> None:
    # "0/0 required mirrors" reads like a divide-by-zero; a registry that
    # exempts every visible mirror deserves an explicit alarm (Copilot, PR #45).
    config = _config(tmp_path, "- [ ] alpha\n")
    (tmp_path / "vault").mkdir()
    config = replace(config, plan_exempt=("vault", "maestro"))
    hit = coverage_hit(config)
    assert hit is not None
    assert "0/0" not in hit.text
    assert "every visible mirror is exempt" in hit.text
    assert "not counted: vault, maestro" in hit.text


def test_a_plan_file_with_nothing_open_still_counts_as_covered(tmp_path: Path) -> None:
    # An all-checked plan file is a maintained plan that happens to be empty, not a
    # missing one — arbiter's April snapshot is the live example.
    config = _config(tmp_path, "- [x] everything shipped\n")
    (tmp_path / "vault").mkdir()
    (tmp_path / "vault" / "TODO.md").write_text("- [ ] vault work\n")
    assert coverage_hit(config) is None


def test_item_text_drops_the_checkbox_marker(tmp_path: Path) -> None:
    # The marker is syntax, not content: it reads as noise once the item is quoted
    # inside a sentence about movement.
    config = _config(tmp_path, "* [ ]   ship the gate\n")
    assert open_items(config)[0].text == "ship the gate"


def test_tags_are_parsed_off_the_item_text(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "- [ ] Package the arbiter client @owner:andrei @blocked_by:Maestro#dogfood "
        '@trigger:"p95 > 200ms or 1M rows"\n',
    )
    item = open_items(config)[0]
    assert item.owner == "andrei"
    assert item.blocked_by == "Maestro#dogfood"
    assert item.trigger == "p95 > 200ms or 1M rows"  # quoted value keeps its spaces
    assert item.text == "Package the arbiter client"  # metadata leaves the prose


def test_untagged_items_carry_no_fields(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] plain item\n")
    item = open_items(config)[0]
    assert (item.owner, item.blocked_by, item.trigger) == (None, None, None)


def test_labelling_an_item_does_not_change_its_identity(tmp_path: Path) -> None:
    # THE point of the parser: keys are normalized text, so labelling 56 items in one
    # pass would otherwise report 56 closed and 56 opened (handoff note 2026-07-26 §4).
    config = _config(tmp_path, "- [ ] ship the gate\n")
    before = open_items(config)[0].key
    (tmp_path / "maestro" / "TODO.md").write_text(
        '- [ ] ship the gate @owner:andrei @trigger:"a week of clean runs"\n'
    )
    assert open_items(config)[0].key == before


def test_editing_a_tag_is_not_a_close_and_reopen(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] ship the gate @owner:andrei\n")
    record(config, "daily", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [ ] ship the gate @owner:someone-else @blocked_by:Maestro#x\n"
    )
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "no plan items opened or closed" in hit.text


def test_every_at_key_value_is_a_tag_and_stripped_from_movement_text(
    tmp_path: Path,
) -> None:
    # The shared plan-fields tokenizer treats every `@key:value` as a tag (not
    # just Robin's three keys), so they are all stripped from the movement text —
    # including an unknown key like `@foo:bar`. This is deliberate: it also strips
    # `@id`, so the PF-2B backfill will not churn text-based keys. A non-tag like
    # `me@host` (no colon) survives verbatim; Robin still surfaces only
    # owner/blocked_by/trigger as fields.
    config = _config(tmp_path, "- [ ] see @owner:andrei about @foo:bar and me@host\n")
    item = open_items(config)[0]
    assert item.owner == "andrei"
    assert item.text == "see about and me@host"


def test_last_occurrence_of_a_repeated_key_wins(tmp_path: Path) -> None:
    # The shared tokenizer is last-wins (Robin's own parser was first-wins); a
    # duplicate @owner on one line is malformed either way and no real plan item
    # carries one, so this only pins the shared behavior.
    config = _config(tmp_path, "- [ ] shared item @owner:andrei @owner:someone-else\n")
    item = open_items(config)[0]
    assert item.owner == "someone-else"
    assert item.text == "shared item"  # both occurrences leave the prose


def test_a_hit_stays_within_its_length_cap_with_fields(tmp_path: Path) -> None:
    # The fields must not smuggle the hit past the 260-char budget — every hit that
    # grows costs the prompt sources it could have carried (Copilot, PR #27).
    long_item = "x" * 400
    config = _config(
        tmp_path, f"- [ ] {long_item} @owner:andrei @blocked_by:Maestro#R-03\n"
    )
    hit = plan_hits(config)[0]
    assert len(hit.text) <= 260
    # prose is the compressible part; the fields survive whole, like the truncation
    # flag in uncommitted() that is placed before the list the cap can eat
    assert "owner: andrei" in hit.text and "blocked by: Maestro#R-03" in hit.text


def test_hits_surface_the_fields_in_plain_language(tmp_path: Path) -> None:
    # Parsed and then hidden would be pointless: the digest reads hit text, so the
    # fields have to reach it — glossed, not as raw tag syntax (AUDIENCE RULE).
    config = _config(
        tmp_path, "- [ ] SDK integration @owner:atp @blocked_by:Maestro#R-03\n"
    )
    text = plan_hits(config)[0].text
    assert "SDK integration" in text and "@owner" not in text
    assert "owner: atp" in text and "blocked by: Maestro#R-03" in text


def test_unowned_items_are_counted_against_the_total(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "- [ ] mine @owner:github:andrei-shtanakov\n"
        "- [ ] nobody's\n- [ ] also nobody's\n",
    )
    hit = fields_hit(config, "daily")
    assert hit is not None and hit.path == "(plan-fields)"
    assert "human-owned=1" in hit.text and "missing=2" in hit.text


def test_typed_ownership_categories_are_independent_from_movement(
    tmp_path: Path,
) -> None:
    config = _config(
        tmp_path,
        "- [ ] human @owner:github:andrei-shtanakov\n"
        "- [ ] team @owner:github-team:example/platform\n"
        "- [ ] repo @owner:repo:maestro\n"
        "- [ ] unknown repo @owner:repo:ghost\n"
        "- [ ] undecided @owner:TBD\n"
        "- [ ] legacy role @owner:tech-lead\n"
        '- [ ] missing but waiting @trigger:"release"\n',
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "human-owned=2" in hit.text
    assert "repo-owned=1" in hit.text
    assert "unknown-repo-owner=1" in hit.text
    assert "TBD=1" in hit.text
    assert "invalid-owner=1" in hit.text
    assert "missing=1" in hit.text
    assert "actionable=6" in hit.text and "waiting-by-trigger=1" in hit.text
    assert "ACTION:" not in hit.text  # missing does not imply ready to move


def test_condition_diagnostics_keep_their_source_across_two_plan_files(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, "- [x] shipped @id:done\n\n")
    (tmp_path / "maestro" / "ROADMAP.md").write_text(
        "# Later\n- [ ] stale @id:next @blocked_by:todo://maestro/done\n"
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "stale-condition=1" in hit.text
    assert "actionable=0" in hit.text


def test_fields_report_reads_each_mirror_plan_set_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, "- [ ] one\n")
    original = plan_state._plan_files
    calls: list[Path] = []

    def counted(root: Path):
        calls.append(root)
        yield from original(root)

    monkeypatch.setattr(plan_state, "_plan_files", counted)
    assert fields_hit(config, "daily") is not None
    assert calls == [config.vault_path, *config.repo_paths]


def test_unowned_items_are_broken_down_by_what_stands_in_their_way(
    tmp_path: Path,
) -> None:
    # One number mixed three different facts (issue #37, triage of the 2026-08-04
    # digest): items nobody picked up, items waiting on a stated condition, and
    # items whose labels the line-based parser cannot even see.
    config = _config(
        tmp_path,
        "- [ ] pick me up\n"
        "- [ ] me too\n"
        "- [ ] waiting @blocked_by:maestro#gate\n"
        '- [ ] watching @trigger:"var file > 50 MB"\n',
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "missing=4" in hit.text
    assert "actionable=2" in hit.text
    assert "waiting-by-trigger=1" in hit.text
    assert "waiting-by-blocker=1" in hit.text


def test_tags_on_a_continuation_line_are_reported_as_malformed(tmp_path: Path) -> None:
    # 2 of the 27 "unowned" items on 2026-08-04 were false positives: their
    # @owner/@id sat on a wrapped continuation line, invisible to the line-based
    # parser. They are a labelling defect, not an unowned item, and folding them
    # into "actionable" misstates the backlog.
    config = _config(
        tmp_path,
        "- [ ] wrapped item whose tags spilled onto the next line\n"
        "  @owner:andrei @id:wrapped-item\n"
        "- [ ] genuinely unowned\n",
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "actionable=2" in hit.text
    assert "WARNING: 1 item(s) put field tags on continuation lines" in hit.text
    assert "continuation line" in hit.text


def test_a_prose_continuation_line_is_not_malformed(tmp_path: Path) -> None:
    # Wrapped prose without tags is just formatting; only invisible tags make an
    # item malformed.
    config = _config(
        tmp_path,
        "- [ ] wrapped item whose prose\n  spills onto the next line\n",
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "actionable=1" in hit.text
    assert "continuation lines" not in hit.text


def test_a_closed_blocker_target_without_an_owner_is_warned_about(
    tmp_path: Path,
) -> None:
    # The condition already fired and nobody owns the reaction — the one state in
    # the unowned set that is both machine-checkable and urgent (issue #37 §2).
    # The repo half of the ref matches case-insensitively: live data carries both
    # `maestro#…` and `Maestro#…` spellings.
    config = _config(tmp_path, "- [x] ship the gate @id:gate\n")
    arbiter = tmp_path / "arbiter"
    arbiter.mkdir()
    (arbiter / "TODO.md").write_text(
        "- [ ] react to the gate @blocked_by:Maestro#gate\n"
    )
    config = RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[tmp_path / "maestro", arbiter],
        var_dir=tmp_path / "var",
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "stale-condition=1" in hit.text


def test_an_open_or_unresolvable_blocker_target_does_not_warn(tmp_path: Path) -> None:
    # An open target means the item is legitimately waiting; a ref that resolves to
    # nothing is ambiguous (typo or removed item) and must not be claimed as fired.
    config = _config(
        tmp_path,
        "- [ ] ship the gate @id:gate\n"
        "- [ ] waiting @blocked_by:maestro#gate\n"
        "- [ ] dangling @blocked_by:maestro#no-such-id\n",
    )
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "stale-condition=0" in hit.text
    assert "waiting-by-blocker=2" in hit.text


def test_a_fully_owned_plan_still_reports_movement(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] mine @owner:github:andrei-shtanakov\n")
    hit = fields_hit(config, "daily")
    assert hit is not None
    assert "human-owned=1" in hit.text and "actionable=1" in hit.text
    assert fields_hit(_config(tmp_path, "- [x] done\n"), "daily") is None


def test_unowned_count_carries_its_previous_value(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n- [ ] beta\n")
    record(config, "daily", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [ ] alpha @owner:github:andrei-shtanakov\n- [ ] beta\n"
    )
    hit = fields_hit(config, "daily")
    assert "missing=1" in hit.text and "was missing=2" in hit.text


def test_an_empty_snapshot_is_a_baseline_of_zero(tmp_path: Path) -> None:
    # A snapshot with no items is not a missing snapshot: the previous unowned count
    # was definitively 0, and hiding that hides the regression (Copilot, PR #28).
    config = _config(tmp_path, "- [x] all done\n")
    record(config, "daily", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text("- [ ] new unowned work\n")
    hit = fields_hit(config, "daily")
    assert "missing=1" in hit.text and "was missing=0" in hit.text


def test_a_snapshot_from_before_owner_tracking_claims_no_movement(
    tmp_path: Path,
) -> None:
    # Entries written before owners were recorded cannot say how many were unowned;
    # reading their silence as "all of them" would invent a regression that never happened.
    config = _config(tmp_path, "- [ ] alpha\n")
    config.var_dir.mkdir(parents=True, exist_ok=True)
    (config.var_dir / "plan-state-daily.json").write_text(
        '{"version": 1, "updated": 1, "items": {"maestro/TODO.md::alpha": '
        '{"text": "alpha", "first_seen": 1, "last_seen": 1}}}'
    )
    hit = fields_hit(config, "daily")
    assert "missing=1" in hit.text
    assert "was" not in hit.text


def test_record_persists_the_owner_field(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha @owner:andrei\n- [ ] beta\n")
    record(config, "daily", now=NOW)
    items = json.loads((config.var_dir / "plan-state-daily.json").read_text())["items"]
    owners = {entry["text"]: entry["owner"] for entry in items.values()}
    assert owners == {"alpha": "andrei", "beta": None}  # key present even when unset


def test_first_run_reports_no_baseline_rather_than_no_movement(tmp_path: Path) -> None:
    # Negative-evidence rule: an absent snapshot is not proof that nothing moved.
    config = _config(tmp_path, "- [ ] alpha\n- [ ] beta\n")
    hit = delta_hit(config, "daily", now=NOW)
    assert hit is not None and hit.path == "(plan-delta)"
    assert "no previous snapshot" in hit.text
    assert "2 open plan item" in hit.text


def test_delta_names_newly_opened_and_closed_items(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n- [ ] beta\n")
    record(config, "daily", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [x] alpha\n- [ ] beta\n- [ ] gamma\n"
    )
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "1 newly opened" in hit.text and "gamma" in hit.text
    assert "1 closed" in hit.text and "alpha" in hit.text
    assert "2 open" in hit.text  # the new total, so coverage movement is visible


def test_unchanged_window_is_reported_as_no_movement(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "no plan items opened or closed" in hit.text


def test_snapshots_are_per_cadence(tmp_path: Path) -> None:
    # A daily run must not consume the weekly baseline, or the weekly digest would
    # report a day's movement as the week's.
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "weekly", now=NOW)
    (tmp_path / "maestro" / "TODO.md").write_text("- [ ] alpha\n- [ ] beta\n")
    record(config, "daily", now=NOW + timedelta(days=1))
    (tmp_path / "maestro" / "TODO.md").write_text(
        "- [ ] alpha\n- [ ] beta\n- [ ] gamma\n"
    )
    daily = delta_hit(config, "daily", now=NOW + timedelta(days=2))
    weekly = delta_hit(config, "weekly", now=NOW + timedelta(days=2))
    assert "1 newly opened" in daily.text  # gamma only
    assert "2 newly opened" in weekly.text  # beta and gamma


def test_long_open_items_are_aged_from_first_sight(tmp_path: Path) -> None:
    # "Stale decisions" without a first-seen timestamp is guesswork; with one it is
    # a fact about how long the item has been sitting open.
    config = _config(tmp_path, "- [ ] ancient decision\n")
    record(config, "weekly", now=NOW)
    later = NOW + timedelta(days=45)
    record(config, "weekly", now=later)
    hit = delta_hit(config, "weekly", now=later)
    assert "open for 45 days" in hit.text and "ancient decision" in hit.text


def test_corrupt_snapshot_degrades_to_first_run(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    (config.var_dir / "plan-state-daily.json").write_text("{not json")
    hit = delta_hit(config, "daily", now=NOW + timedelta(days=1))
    assert "no previous snapshot" in hit.text  # never a false "nothing moved"


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 1, "items": {"k": "not-an-entry"}}',  # entry is not a mapping
        '{"version": 1, "items": {"k": {"text": "a"}}}',  # no first_seen to age from
        '{"version": 1, "items": {"k": {"first_seen": 100}}}',  # no text to name
        '{"version": 1, "items": {"k": {"text": "a", "first_seen": "yesterday"}}}',
        '{"version": 99, "items": {}}',  # written by a future format
        '{"version": 1, "items": []}',  # items is not a mapping
    ],
)
def test_structurally_invalid_snapshot_degrades_to_first_run(
    tmp_path: Path, payload: str
) -> None:
    # Parsing is not validation: a snapshot that loads but has the wrong shape used to
    # crash the next delta instead of degrading (Copilot review, PR #21).
    config = _config(tmp_path, "- [ ] alpha\n")
    config.var_dir.mkdir(parents=True, exist_ok=True)
    (config.var_dir / "plan-state-daily.json").write_text(payload)
    hit = delta_hit(config, "daily", now=NOW)
    assert "no previous snapshot" in hit.text
    record(config, "daily", now=NOW)  # and recording over it must not crash either


def test_record_carries_first_seen_across_runs(tmp_path: Path) -> None:
    config = _config(tmp_path, "- [ ] alpha\n")
    record(config, "daily", now=NOW)
    record(config, "daily", now=NOW + timedelta(days=3))
    state = json.loads((config.var_dir / "plan-state-daily.json").read_text())
    item = next(iter(state["items"].values()))
    assert item["first_seen"] == int(NOW.timestamp())  # age is not reset by re-sighting
