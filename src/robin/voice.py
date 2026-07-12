"""Voice pipeline (ROBIN-SPEC slot 21): transcribe-before-processing, synthesize replies.

Provider-pluggable behind two tiny protocols; the default implementation is OpenAI
(whisper-1 STT, tts-1 TTS with Opus output — valid for Telegram sendVoice). Secrets come
from env (OPENAI_API_KEY), never the KB (slot 17)."""

from __future__ import annotations

import io
import re
from typing import Protocol

from .config import RobinConfig

# Rough server-side cost estimates so /cost is not blind to voice spend (§7). Whisper is
# billed per minute, tts-1 per character; logged as estimates, refined later.
STT_USD_PER_MIN = 0.006
TTS_USD_PER_1K_CHARS = 0.015


class Stt(Protocol):
    def transcribe(self, audio: bytes, mime: str) -> str: ...


class Tts(Protocol):
    def synthesize(self, text: str) -> bytes: ...  # OGG/Opus bytes


_EXT_BY_MIME = {
    "audio/ogg": "ogg",
    "audio/opus": "ogg",
    "audio/webm": "webm",
    "video/webm": "webm",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
}


class OpenAiStt:
    def __init__(self, model: str = "whisper-1") -> None:
        from openai import OpenAI  # deferred: adapters can run without the key

        self._client = OpenAI()
        self._model = model

    def transcribe(self, audio: bytes, mime: str) -> str:
        ext = _EXT_BY_MIME.get(mime.split(";")[0].strip().lower(), "ogg")
        buffer = io.BytesIO(audio)
        buffer.name = f"voice.{ext}"  # the API infers the codec from the filename
        result = self._client.audio.transcriptions.create(
            model=self._model, file=buffer
        )
        return result.text.strip()


class OpenAiTts:
    def __init__(self, voice: str, model: str = "tts-1") -> None:
        from openai import OpenAI

        self._client = OpenAI()
        self._voice = voice
        self._model = model

    def synthesize(self, text: str) -> bytes:
        response = self._client.audio.speech.create(
            model=self._model, voice=self._voice, input=text, response_format="opus"
        )
        return response.content


def make_stt(config: RobinConfig) -> Stt:
    if config.stt_provider == "openai":
        return OpenAiStt(model=config.stt_model)
    raise ValueError(f"unknown STT provider: {config.stt_provider}")


def make_tts(config: RobinConfig) -> Tts:
    if config.tts_provider == "openai":
        return OpenAiTts(voice=config.tts_voice, model=config.tts_model)
    raise ValueError(f"unknown TTS provider: {config.tts_provider}")


def speakable(answer_text: str, max_chars: int = 900) -> str:
    """Strip markup and citations for the spoken reply; keep it short — the full cited
    answer is always delivered as text alongside."""
    text = answer_text
    text = re.sub(r"`[^`]{1,200}`", "", text)  # inline code / path:line cites
    text = re.sub(r"^\s*(?:•|-|\*)\s.*$", "", text, flags=re.M)  # bullet source lists
    text = re.sub(r"[*_#>]+", "", text)
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > max_chars:
        cut = text[:max_chars]
        text = cut[: cut.rfind(".") + 1] or cut
    return text
