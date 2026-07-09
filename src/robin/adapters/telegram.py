"""Telegram chat adapter (ROBIN-SPEC slot 1): long polling, DMs + group @mentions.

Thin wrapper over robin.agent.ask(). Gate order: allowlist → §7 caps → (voice: STT) → ask.
§6.7: everything sent as HTML is escaped first; a parser-rejected send is a LOGGED failure
followed by a plain-text retry, never a silent fallback. Chat content is untrusted — there
are no config-changing commands (§6.5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Message, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .. import fmt, guard, memory
from ..agent import Answer, ask
from ..changes import parse_period
from ..config import RobinConfig, load_config
from ..voice import Stt, Tts, make_stt, make_tts, speakable
from ..log import setup_logging

logger = logging.getLogger("robin.telegram")

_HELP = (
    "I'm Robin — I answer questions about the AI-Orchestrators ecosystem from the "
    "knowledge repo, with citations.\n\n"
    "Ask in text or send a voice note (any language). Examples:\n"
    "• what is the arbiter repo for?\n"
    "• что изменилось за неделю?\n"
    "• /digest today | week | since 2026-07-01\n"
    "• /cost — today's spend and quotas"
)


@dataclass
class Runtime:
    """Lazily-built shared state for handlers (kept off the module globals for tests)."""

    config: RobinConfig
    stt: Stt | None = None
    tts: Tts | None = None


def _requester(update: Update) -> str:
    user = update.effective_user
    return str(user.id) if user else "(unknown)"


def _allowed(config: RobinConfig, update: Update) -> bool:
    """Slot 7 identity registry: numeric ids (recommended) or @usernames; empty = open."""
    if not config.allowed_dm_users:
        return True
    user = update.effective_user
    if user is None:
        return False
    candidates = {str(user.id)}
    if user.username:
        candidates |= {user.username, f"@{user.username}"}
    return bool(candidates & set(config.allowed_dm_users))


def gate(config: RobinConfig, update: Update) -> str | None:
    """Refusal text, or None to proceed. Every refusal is user-visible and short."""
    if not _allowed(config, update):
        logger.info("refused non-allowlisted user %s", _requester(update))
        return "Sorry, I only answer registered team members. Ask the maintainer for access."
    try:
        guard.check(config, _requester(update))
    except guard.BudgetExceeded:
        return "Daily budget is spent — I'll be back after midnight. (§7 cost cap)"
    except guard.RateLimited:
        return "You've hit today's message quota — try again tomorrow. (§7 rate limit)"
    return None


def _addressed_text(
    message: Message, bot_username: str, bot_id: int | None = None
) -> str | None:
    """DM text is always addressed; in groups an @mention (stripped) or a reply to one of
    the bot's own messages is. Replies work even with BotFather privacy mode ON."""
    text = message.text or ""
    if message.chat.type == ChatType.PRIVATE:
        return text
    mention = f"@{bot_username}"
    if mention.lower() in text.lower():
        cleaned = text.replace(mention, "").strip()
        return cleaned or None
    reply = getattr(message, "reply_to_message", None)
    if (
        reply is not None
        and getattr(reply, "from_user", None) is not None
        and bot_id is not None
        and reply.from_user.id == bot_id
    ):
        return text.strip() or None
    return None


async def _send_html(message: Message, html: str) -> None:
    """§6.7: send as HTML; on parser rejection LOG the failure, then retry as plain text."""
    for part in fmt.chunk(html):
        try:
            await message.reply_text(part, parse_mode=ParseMode.HTML)
        except BadRequest as exc:
            logger.error("§6.7 formatting-rejected send: %s | payload=%r", exc, part[:200])
            await message.reply_text(part)


async def _answer(update: Update, runtime: Runtime, question: str, *, voice_reply: bool) -> None:
    message = update.effective_message
    chat_id = str(update.effective_chat.id)
    config = runtime.config
    history = memory.recent(config, "telegram", chat_id)
    await message.chat.send_action("typing")
    try:
        answer: Answer = ask(
            question,
            config,
            surface="telegram",
            requester=_requester(update),
            history=history,
        )
    except Exception:
        logger.exception("ask() failed")
        await message.reply_text("Something went wrong while composing the answer — logged.")
        return
    memory.append(config, "telegram", chat_id, "user", question)
    memory.append(config, "telegram", chat_id, "robin", answer.text or "")
    await _send_html(message, fmt.render_answer(answer))
    if voice_reply and runtime.tts is not None and answer.text:
        spoken = speakable(answer.text)
        if spoken:
            try:
                await message.reply_voice(runtime.tts.synthesize(spoken))
            except Exception:
                logger.exception("TTS reply failed (text answer already delivered)")


def build_application(runtime: Runtime) -> Application:
    config = runtime.config
    if not config.telegram_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set (slot 17: secrets via env)")
    app = Application.builder().token(config.telegram_token).build()

    async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(_HELP)

    async def cmd_cost(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        # §7 observability: exempt from caps (it must work when the budget is spent),
        # but still allowlist-only.
        if _allowed(config, update):
            await update.effective_message.reply_text(guard.cost_report(config))

    async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if (refusal := gate(config, update)) is not None:
            await update.effective_message.reply_text(refusal)
            return
        arg = " ".join(context.args) if context.args else "week"
        phrase = {"today": "today", "week": "this week"}.get(arg, arg)
        if parse_period(phrase, tz=config.tz) is None:
            await update.effective_message.reply_text(
                "Usage: /digest today | week | since YYYY-MM-DD"
            )
            return
        await _answer(
            update, runtime, f"What changed {phrase}? Summarize per repo.", voice_reply=False
        )

    async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        question = _addressed_text(
            update.effective_message, context.bot.username, context.bot.id
        )
        if question is None:
            return  # group chatter not addressed to us (slot 8 passive capture: not enabled)
        if (refusal := gate(config, update)) is not None:
            await update.effective_message.reply_text(refusal)
            return
        await _answer(update, runtime, question, voice_reply=False)

    async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        if message.chat.type != ChatType.PRIVATE:
            return  # voice Q&A is DM-only; group audio is ambient noise
        if (refusal := gate(config, update)) is not None:
            await message.reply_text(refusal)
            return
        if runtime.stt is None:
            await message.reply_text("Voice is not configured (missing OPENAI_API_KEY).")
            return
        media = message.voice or message.audio
        file = await context.bot.get_file(media.file_id)
        audio = bytes(await file.download_as_bytearray())
        mime = media.mime_type or "audio/ogg"
        try:
            question = runtime.stt.transcribe(audio, mime)
        except Exception:
            logger.exception("STT failed")
            await message.reply_text("Could not transcribe that — try again or type it.")
            return
        if not question:
            await message.reply_text("I heard silence — try again?")
            return
        # slot 21: transcribed content is a question, NEVER parsed as a command.
        await message.reply_text(f"🎙 {question}")
        await _answer(update, runtime, question, voice_reply=True)

    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    return app


def main() -> None:
    setup_logging()
    config = load_config()
    runtime = Runtime(config=config)
    try:
        runtime.stt, runtime.tts = make_stt(config), make_tts(config)
    except Exception as exc:  # voice is optional; text Q&A must not die without it
        logger.warning("voice disabled: %s", exc)
    build_application(runtime).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
