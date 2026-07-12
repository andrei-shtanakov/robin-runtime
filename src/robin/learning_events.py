"""LearningEvent v1 emission — the observational half of RD-007.

Maps gap records (var/gaps.jsonl) to LearningEvent-v1 records and APPENDS
them to robin's own ``var/learning_events.jsonl``. That is the entire write
surface of this module: events are observational and never mutate governed
knowledge; graduation to durable artifacts (eval cases, KB notes, prompt
changes) happens exclusively through human-reviewed PRs
(atp-platform docs/2026-07-12-rd-007-learning-event-design.md §0, §6).

The contract is the vendored pinned copy at
``contracts/learning-event-v1.schema.json`` (SSOT: atp-platform
method/contract/). Emitted records conform by construction; the test suite
validates them against the vendored schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from robin.config import RobinConfig

EVENTS_FILE = "learning_events.jsonl"
PRODUCER = "robin-runtime"
GAPS_STORE = "var/gaps.jsonl"

# Crockford base32 (no I, L, O, U) — ULID alphabet.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

# fail_signal -> graduation target class (design §5: reformulations feed the
# prompt's synonym/rewrite hints; everything else is KB material by default).
_TARGETS = {"reformulation": "prompt"}
_DEFAULT_TARGET = "kb"


def mint_event_id(now_ms: int | None = None) -> str:
    """A ULID: 48-bit millisecond timestamp + 80 random bits, 26 chars."""
    ts = int(time.time() * 1000) if now_ms is None else now_ms
    value = (ts & (2**48 - 1)) << 80 | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 0x1F])
        value >>= 5
    return "".join(reversed(chars))


def gap_to_event(gap: dict) -> dict:
    """Map one gaps.jsonl record to a LearningEvent v1 record.

    ``source.id`` is content-addressed (sha256 of the canonical gap JSON):
    gap records carry no id of their own, and a content hash keeps the
    provenance pointer stable and idempotent across re-reads.
    """
    canonical = json.dumps(gap, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    ts = datetime.fromtimestamp(int(gap.get("ts", 0)), tz=timezone.utc)
    fail_signal = gap.get("fail_signal") or "unknown"
    return {
        "schema_version": "1",
        "event_id": mint_event_id(),
        "producer": PRODUCER,
        "kind": "gap",
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {"store": GAPS_STORE, "id": f"sha256:{digest[:32]}"},
        "proposed_target": _TARGETS.get(fail_signal, _DEFAULT_TARGET),
        "payload": {
            "question": gap.get("question"),
            "fail_signal": fail_signal,
            "answer_class": gap.get("answer_class"),
            "surface": gap.get("surface"),
        },
    }


def emit_events(config: RobinConfig, gaps: list[dict]) -> Path:
    """Append one LearningEvent per gap to var/learning_events.jsonl."""
    config.var_dir.mkdir(parents=True, exist_ok=True)
    path = config.var_dir / EVENTS_FILE
    with path.open("a") as handle:
        for gap in gaps:
            handle.write(json.dumps(gap_to_event(gap), ensure_ascii=False) + "\n")
    return path
