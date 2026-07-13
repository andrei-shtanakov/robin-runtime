"""M4 learning loop (§5, §6.4): staged writes with read-back, human promotion routing,
promoted rules loaded into the system prompt."""

from __future__ import annotations

from pathlib import Path

import pytest

from robin.config import RobinConfig
from robin.learnings import (
    list_staged,
    load_promoted,
    promote,
    reject,
    stage,
)


def _config(tmp_path: Path) -> RobinConfig:
    return RobinConfig(vault_path=tmp_path, repo_paths=[], var_dir=tmp_path / "var")


def test_stage_writes_dated_file_and_reads_back(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = stage(
        config,
        question="what changed this week?",
        comment="дайджест должен считать неделю с понедельника",
        fail_signal="gap_command",
        surface="telegram",
        requester="42",
    )
    assert path.is_file() and path.parent.name == "staged"
    content = path.read_text()
    assert "what changed this week?" in content
    assert "дайджест должен считать неделю с понедельника" in content
    assert "gap_command" in content
    assert list_staged(config) == [path]


def test_stage_never_appends_one_insight_per_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = stage(
        config,
        question="q",
        comment="same comment",
        fail_signal="thumbs_down",
        surface="telegram",
    )
    second = stage(
        config,
        question="q",
        comment="same comment",
        fail_signal="thumbs_down",
        surface="telegram",
    )
    assert first != second and first.is_file() and second.is_file()


def test_stage_survives_missing_question_and_comment(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = stage(
        config,
        question=None,
        comment=None,
        fail_signal="thumbs_down",
        surface="telegram",
    )
    assert "human to fill in" in path.read_text()


def test_promote_memory_route_enters_promoted_rules(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = stage(
        config,
        question="q",
        comment="always cite the ADR date",
        fail_signal="gap_command",
        surface="telegram",
    )
    assert load_promoted(config) == []  # staged is NOT loaded — promotion is the gate
    destination = promote(config, path.name, "memory")
    assert destination.parent.name == "promoted" and not path.exists()
    (rule,) = load_promoted(config)
    assert "always cite the ADR date" in rule and "\n" not in rule
    # staging boilerplate must not leak into the system prompt
    assert "Promote (human" not in rule and "Staged learning" not in rule


def test_promote_skill_and_kb_routes_go_to_routed_not_promoted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for route in ("skill", "kb"):
        path = stage(
            config,
            question="q",
            comment=f"route me via {route}",
            fail_signal="gap_command",
            surface="telegram",
        )
        destination = promote(config, path.name, route)
        assert destination.parent.name == "routed"
    assert load_promoted(config) == []  # only the memory route feeds sessions


def test_reject_archives_without_deleting(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = stage(
        config,
        question="q",
        comment="bad idea",
        fail_signal="gap_command",
        surface="telegram",
    )
    destination = reject(config, path.name)
    assert destination.is_file() and destination.parent.name == "rejected"
    assert not path.exists() and list_staged(config) == []


def test_promote_rejects_unknown_route_and_hostile_names(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = stage(
        config, question="q", comment="c", fail_signal="gap_command", surface="telegram"
    )
    with pytest.raises(ValueError):
        promote(config, path.name, "chat")
    # path traversal in <name> must not escape staged/
    (tmp_path / "outside.md").write_text("secret")
    with pytest.raises(FileNotFoundError):
        promote(config, "../../outside.md", "memory")
    assert (tmp_path / "outside.md").exists()


def test_promoted_rules_enter_system_prompt(tmp_path: Path) -> None:
    from robin.agent import _system_prompt

    config = _config(tmp_path)
    assert "PROMOTED TEAM LEARNINGS" not in _system_prompt(config)
    path = stage(
        config,
        question="q",
        comment="answer digest questions in Monday-start weeks",
        fail_signal="gap_command",
        surface="telegram",
    )
    promote(config, path.name, "memory")
    prompt = _system_prompt(config)
    assert "PROMOTED TEAM LEARNINGS" in prompt
    assert "Monday-start weeks" in prompt
