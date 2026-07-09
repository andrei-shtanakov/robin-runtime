"""Telegram/web output rendering (§6.7): escape user/repo-derived content BEFORE adding
our own markup; chunk to Telegram's 4096-char message limit without splitting tags."""

from __future__ import annotations

from .agent import Answer

TELEGRAM_LIMIT = 4096


def escape_html(text: str) -> str:
    """Escape for Telegram HTML parse mode. Applied to ALL derived text (§6.7)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_answer(answer: Answer, *, max_cites: int = 5) -> str:
    """Answer text + a short source list, as Telegram-safe HTML."""
    parts = [escape_html(answer.text or "")]
    if answer.sources:
        cites = [
            f"• <code>{escape_html(hit.path)}:{hit.line}</code>"
            for hit in answer.sources[:max_cites]
        ]
        parts += ["", "<b>Sources</b>", *cites]
    return "\n".join(parts).strip()


def chunk(html: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on paragraph, then line boundaries; hard-split only as a last resort.
    Escaped entities/tags in our output never contain newlines, so boundary splits are safe."""
    if len(html) <= limit:
        return [html] if html else []
    chunks: list[str] = []
    current = ""
    for paragraph in html.split("\n\n"):
        candidate = f"{current}\n\n{paragraph}" if current else paragraph
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(paragraph) <= limit:
            current = paragraph
            continue
        for line in paragraph.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= limit:
                current = candidate
                continue
            if current:
                chunks.append(current)
                current = ""
            while len(line) > limit:  # pathological single line: hard split
                chunks.append(line[:limit])
                line = line[limit:]
            current = line
    if current:
        chunks.append(current)
    return chunks
