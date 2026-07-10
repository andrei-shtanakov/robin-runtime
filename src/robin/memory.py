"""Per-chat rolling conversation window (ROBIN-SPEC slots 12-14).

Every ask() runs a fresh isolated session (§6.5), so the spec's idle-window rule reduces to:
always reseed the last N turns. One JSONL per chat under Robin's own store — never the KB."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .agent import Turn
from .config import RobinConfig

_MAX_TURN_CHARS = 500


def _chat_file(config: RobinConfig, surface: str, chat_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{surface}-{chat_id}")
    return config.var_dir / "chats" / f"{safe}.jsonl"


def recent(config: RobinConfig, surface: str, chat_id: str, n: int | None = None) -> list[Turn]:
    """Last N turns for this chat (unconditional reseed; slots 12/14)."""
    n = n if n is not None else config.history_turns
    path = _chat_file(config, surface, chat_id)
    if not path.is_file():
        return []
    turns: list[Turn] = []
    for line in path.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        role, text = record.get("role"), record.get("text")
        if isinstance(role, str) and isinstance(text, str):
            turns.append(Turn(role=role, text=text))
    return turns[-n:]


def last_user_turn(
    config: RobinConfig, surface: str, chat_id: str
) -> tuple[str, int] | None:
    """(text, ts) of the chat's most recent user turn — the reformulation detector
    (stage 2, gaps.py) compares the incoming question against it."""
    path = _chat_file(config, surface, chat_id)
    if not path.is_file():
        return None
    result: tuple[str, int] | None = None
    for line in path.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("role") == "user" and isinstance(record.get("text"), str):
            result = (record["text"], int(record.get("ts", 0)))
    return result


def append(config: RobinConfig, surface: str, chat_id: str, role: str, text: str) -> None:
    """Record one turn (truncated — the window is continuity context, not an archive)."""
    path = _chat_file(config, surface, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": int(time.time()), "role": role, "text": text[:_MAX_TURN_CHARS]}
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
