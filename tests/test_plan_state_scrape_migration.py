"""PF-7: plan_state now scrapes via the shared plan-fields package.

Three guards on the migration (ADR-ECO-005):
  * characterization — the pre-migration scraper and the new one produce
    byte-identical `(path, section, text, key, owner, blocked_by, trigger, line)`
    on realistic plan text, so no snapshot key moves;
  * mutation — breaking the package tokenizer actually breaks that equivalence,
    proving plan_state really depends on the shared package (not a private copy);
  * golden — a snapshot taken with the new scraper reports zero opened/closed
    against itself, so the migration run itself moves nothing.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

from robin.config import RobinConfig
from robin.plan_state import _key, delta_hit, open_items, record

FIXTURE = (Path(__file__).parent / "fixtures" / "plan" / "mixed.md").read_text()
NOW = datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc)


# --- frozen copy of the pre-migration scraper (the reference oracle) ----------
_OLD_UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]\s+(\S.*)$")
_OLD_HEADING = re.compile(r"^#{1,6}\s+(.+)")
_OLD_TAG = re.compile(r'(?:(?<=\s)|^)@(owner|blocked_by|trigger):(?:"([^"]*)"|(\S+))')


def _old_parse_tags(line: str) -> tuple[str, dict[str, str]]:
    fields: dict[str, str] = {}
    for m in _OLD_TAG.finditer(line):
        value = m.group(2) if m.group(2) is not None else m.group(3)
        fields.setdefault(m.group(1), value)
    return " ".join(_OLD_TAG.sub("", line).split()), fields


def _old_open_items(text: str, rel: str) -> list[tuple]:
    out: list[tuple] = []
    heading = ""
    for number, line in enumerate(text.splitlines(), 1):
        if m := _OLD_HEADING.match(line):
            heading = m.group(1).strip().strip("*").strip()[:60]
            continue
        if m := _OLD_UNCHECKED.match(line):
            text_, fields = _old_parse_tags(m.group(1).strip())
            out.append(
                (
                    _key(rel, text_),
                    rel,
                    number,
                    heading,
                    text_,
                    fields.get("owner"),
                    fields.get("blocked_by"),
                    fields.get("trigger"),
                )
            )
    return out


def _new_tuples(config: RobinConfig) -> list[tuple]:
    return [
        (i.key, i.path, i.line, i.heading, i.text, i.owner, i.blocked_by, i.trigger)
        for i in open_items(config)
    ]


def _config(tmp_path: Path) -> RobinConfig:
    repo = tmp_path / "maestro"
    repo.mkdir()
    (repo / "TODO.md").write_text(FIXTURE)
    return RobinConfig(
        vault_path=tmp_path / "vault",
        repo_paths=[repo],
        var_dir=tmp_path / "var",
    )


def test_characterization_new_scraper_matches_old_byte_for_byte(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    old = _old_open_items(FIXTURE, "maestro/TODO.md")
    assert _new_tuples(config) == old
    # sanity: the fixture actually exercised open items, headings, and tags
    assert len(old) == 7  # open items only; the two [x]/[X] lines are excluded
    assert any(o[5] == "andrei" for o in old)  # an owner was parsed
    assert any("Blocked work" == o[3] for o in old)  # a **bold** heading resolved


def test_backtick_prose_tag_is_not_treated_as_a_field(tmp_path: Path) -> None:
    # the case that drove the tokenizer boundary fix: tags NAMED in prose
    config = _config(tmp_path)
    prose = next(i for i in open_items(config) if "graph has 15" in i.text)
    assert prose.owner == "andrei"
    assert prose.blocked_by is None and prose.trigger is None
    assert "`@blocked_by:`" in prose.text and "`@trigger:`" in prose.text


def test_mutation_breaking_the_package_tokenizer_breaks_equivalence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Prove plan_state truly rides the shared tokenizer: cripple it and the
    # characterization equivalence must fail (otherwise Robin isn't using it).
    config = _config(tmp_path)
    old = _old_open_items(FIXTURE, "maestro/TODO.md")
    import plan_fields.scrape as scrape

    monkeypatch.setattr(scrape, "_TAG_RE", re.compile(r"\Znevermatch"))
    assert _new_tuples(config) != old  # owners/text no longer stripped -> differs


def test_golden_snapshot_reports_zero_churn_against_itself(tmp_path: Path) -> None:
    config = _config(tmp_path)
    record(config, "daily", now=NOW)  # baseline from the new scraper
    movement = delta_hit(config, "daily", now=NOW)
    assert movement is not None
    body = movement.text
    assert "no plan items opened or closed" in body
    assert "newly opened" not in body and "closed or removed" not in body
