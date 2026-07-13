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


def recent(
    config: RobinConfig, surface: str, chat_id: str, n: int | None = None
) -> list[Turn]:
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


def append(
    config: RobinConfig, surface: str, chat_id: str, role: str, text: str
) -> None:
    """Record one turn (truncated — the window is continuity context, not an archive)."""
    path = _chat_file(config, surface, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": int(time.time()), "role": role, "text": text[:_MAX_TURN_CHARS]}
    with path.open("a") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- Channel ambient log (ROBIN-SPEC slot 8 / §6.2) ------------------------------------
# Raw channel messages, the first stage of the §5 memory pipeline. Distinct from the
# conversation window above: it records what the TEAM said around Robin, not exchanges
# with Robin, and it is only ever read back as untrusted ambient context for mentions.


_CHANNEL_KEEP = 200  # rolling bound — a raw channel log is a window, not an archive


def _channel_file(config: RobinConfig, surface: str, chat_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", f"{surface}-{chat_id}")
    return config.var_dir / "channel" / f"{safe}.jsonl"


def _one_line(value: str) -> str:
    """Collapse all whitespace runs (incl. newlines) — a channel message must stay one
    prompt bullet; a multi-line message must not be able to fake other prompt blocks."""
    return re.sub(r"\s+", " ", value).strip()


def log_channel(
    config: RobinConfig, surface: str, chat_id: str, sender: str, text: str
) -> None:
    """Record one channel message (truncated; capture scope is gated by the caller)."""
    path = _channel_file(config, surface, chat_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": int(time.time()),
        "sender": _one_line(sender)[:100],
        "text": _one_line(text)[:_MAX_TURN_CHARS],
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    if len(lines) > _CHANNEL_KEEP:
        path.write_text("\n".join(lines[-_CHANNEL_KEEP:]) + "\n", encoding="utf-8")


def recent_channel(
    config: RobinConfig, surface: str, chat_id: str, n: int | None = None
) -> list[str]:
    """Last N channel messages as "sender: text" lines, oldest first (slot 13)."""
    n = n if n is not None else config.ambient_messages
    path = _channel_file(config, surface, chat_id)
    if n <= 0 or not path.is_file():
        return []
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        sender, text = record.get("sender"), record.get("text")
        if isinstance(sender, str) and isinstance(text, str):
            lines.append(f"{sender}: {text}")
    return lines[-n:]
