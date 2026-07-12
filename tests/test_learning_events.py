"""LearningEvent v1 emission from the gaps store (RD-007 M1b).

Events are observational: this module only APPENDS to robin's own var/ store.
Graduation to durable artifacts happens exclusively via reviewed PR — there is
no code path from here to any governed file (design §0, §6).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from robin import gaps, learning_events, selfreview
from robin.config import RobinConfig

SCHEMA = json.loads(
    (
        Path(__file__).parent.parent / "contracts" / "learning-event-v1.schema.json"
    ).read_text(encoding="utf-8")
)

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)


def _config(tmp_path: Path) -> RobinConfig:
    vault = tmp_path / "vault"
    vault.mkdir(exist_ok=True)
    return RobinConfig(vault_path=vault, repo_paths=[], var_dir=tmp_path / "var")


def _gap(ts: int = 1783869000, signal: str = "zero_retrieval") -> dict:
    return {
        "ts": ts,
        "surface": "telegram",
        "chat": "1",
        "requester": "u1",
        "question": "как настроить gate-check?",
        "retrieval_hits": 0,
        "answer_class": "kb",
        "fail_signal": signal,
        "comment": None,
    }


def _assert_conforms(event: dict) -> None:
    """Structural conformance against the vendored schema (stdlib only)."""
    for key in SCHEMA["required"]:
        assert key in event, f"missing required field {key}"
    assert set(event) <= set(SCHEMA["properties"]), "unexpected top-level field"
    assert event["schema_version"] == SCHEMA["properties"]["schema_version"]["const"]
    assert re.fullmatch(
        SCHEMA["properties"]["event_id"]["pattern"], event["event_id"]
    ), event["event_id"]
    assert event["kind"] in SCHEMA["properties"]["kind"]["enum"]
    if "proposed_target" in event:
        assert (
            event["proposed_target"] in SCHEMA["properties"]["proposed_target"]["enum"]
        )
    for key in SCHEMA["properties"]["source"]["required"]:
        assert event["source"][key]


# ------------------------------------------------------------------ mint


def test_mint_event_id_is_valid_crockford_ulid() -> None:
    seen = {learning_events.mint_event_id() for _ in range(64)}
    assert len(seen) == 64, "event ids must be unique"
    for event_id in seen:
        assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", event_id), event_id


# ------------------------------------------------------------------ mapping


def test_gap_maps_to_conformant_event() -> None:
    event = learning_events.gap_to_event(_gap())
    _assert_conforms(event)
    assert event["producer"] == "robin-runtime"
    assert event["kind"] == "gap"
    assert event["source"]["store"] == "var/gaps.jsonl"
    assert event["payload"]["fail_signal"] == "zero_retrieval"
    assert event["ts"] == "2026-07-12T15:10:00Z"


def test_source_id_is_content_addressed_and_stable() -> None:
    # gaps.jsonl records carry no id — the pointer is a content hash, so the
    # same record always maps to the same source id (idempotent provenance).
    first = learning_events.gap_to_event(_gap())
    second = learning_events.gap_to_event(_gap())
    assert first["source"]["id"] == second["source"]["id"]
    assert first["source"]["id"].startswith("sha256:")
    assert first["event_id"] != second["event_id"]  # events remain distinct


def test_proposed_target_mapping() -> None:
    assert learning_events.gap_to_event(_gap())["proposed_target"] == "kb"
    reform = learning_events.gap_to_event(_gap(signal="reformulation"))
    assert reform["proposed_target"] == "prompt"


# ------------------------------------------------------------------ emission


def test_emit_events_appends_jsonl(tmp_path: Path) -> None:
    config = _config(tmp_path)
    path = learning_events.emit_events(config, [_gap(), _gap(signal="thumbs_down")])
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    for event in lines:
        _assert_conforms(event)
    # append-only: a second emission adds, never rewrites
    learning_events.emit_events(config, [_gap()])
    assert len(path.read_text().splitlines()) == 3


def test_selfreview_run_emits_events(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    gaps.log_gap(
        config,
        surface="telegram",
        chat="1",
        question="что изменилось вчера?",
        fail_signal="zero_retrieval",
        answer_class="temporal",
    )
    monkeypatch.setattr("robin.selfreview.load_config", lambda: config)
    # the [since, until) window excludes a gap logged in the same second as
    # "now" — pin an all-inclusive window; windowing itself is tested elsewhere
    monkeypatch.setattr("robin.selfreview.window", lambda config, now=None: (0, 2**31))

    async def _no_post(*_a, **_kw):
        return None

    monkeypatch.setattr("robin.selfreview.post", _no_post)
    selfreview.run()
    store = config.var_dir / "learning_events.jsonl"
    assert store.exists(), "selfreview.run must emit learning events"
    events = [json.loads(line) for line in store.read_text().splitlines()]
    assert len(events) == 1
    _assert_conforms(events[0])
