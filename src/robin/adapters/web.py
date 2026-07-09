"""Web chat adapter: FastAPI app served by uvicorn behind nginx on the VPS.

Same pipeline as Telegram: gate → (voice: STT) → ask → rendered answer (+ TTS audio).
Auth: static Bearer token from ROBIN_WEB_TOKEN (slot 17), compared with hmac. The page is
a single self-contained HTML file with a MediaRecorder mic button."""

from __future__ import annotations

import base64
import hmac
import logging
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import fmt, guard, memory
from ..agent import ask
from ..config import RobinConfig, load_config
from ..voice import Stt, Tts, make_stt, make_tts, speakable
from ..log import setup_logging

logger = logging.getLogger("robin.web")

_STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Robin", docs_url=None, redoc_url=None)


@lru_cache(maxsize=1)
def _config() -> RobinConfig:
    return load_config()


@lru_cache(maxsize=1)
def _voice() -> tuple[Stt | None, Tts | None]:
    config = _config()
    try:
        return make_stt(config), make_tts(config)
    except Exception as exc:
        logger.warning("voice disabled: %s", exc)
        return None, None


def require_token(authorization: str = Header(default="")) -> None:
    expected = _config().web_token
    if not expected:
        raise HTTPException(503, "ROBIN_WEB_TOKEN is not configured")
    provided = authorization.removeprefix("Bearer ").strip()
    if not (provided and hmac.compare_digest(provided, expected)):
        raise HTTPException(401, "invalid token")


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    chat_id: str = Field(default="default", max_length=64)


def _gate(requester: str) -> None:
    try:
        guard.check(_config(), requester)
    except guard.BudgetExceeded as exc:
        raise HTTPException(429, f"daily budget spent: {exc}") from exc
    except guard.RateLimited as exc:
        raise HTTPException(429, f"rate limited: {exc}") from exc


def _answer(question: str, chat_id: str) -> dict:
    config = _config()
    requester = f"web:{chat_id}"
    _gate(requester)
    history = memory.recent(config, "web", chat_id)
    answer = ask(question, config, surface="web", requester=requester, history=history)
    memory.append(config, "web", chat_id, "user", question)
    memory.append(config, "web", chat_id, "robin", answer.text or "")
    return {
        "answer_html": fmt.render_answer(answer),
        "sources": [f"{hit.path}:{hit.line}" for hit in answer.sources[:8]],
        "cost_usd": answer.cost_usd,
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.post("/api/ask", dependencies=[Depends(require_token)])
def api_ask(request: AskRequest) -> dict:
    return _answer(request.text, request.chat_id)


@app.post("/api/ask-voice", dependencies=[Depends(require_token)])
async def api_ask_voice(
    audio: UploadFile = File(...), chat_id: str = Form(default="default")
) -> dict:
    stt, tts = _voice()
    if stt is None:
        raise HTTPException(503, "voice is not configured on the server")
    payload = await audio.read()
    if not payload:
        raise HTTPException(400, "empty audio")
    try:
        # slot 21: transcribe-before-processing; the transcript is a question, never a command
        transcript = stt.transcribe(payload, audio.content_type or "audio/webm")
    except Exception as exc:
        logger.exception("STT failed")
        raise HTTPException(502, "transcription failed") from exc
    if not transcript:
        raise HTTPException(422, "no speech detected")
    result = _answer(transcript, chat_id)
    result["transcript"] = transcript
    if tts is not None:
        spoken = speakable(_plain(result))
        if spoken:
            try:
                result["audio_b64"] = base64.b64encode(tts.synthesize(spoken)).decode()
            except Exception:
                logger.exception("TTS failed (text answer already composed)")
    return result


def _plain(result: dict) -> str:
    # TTS wants the raw answer, not HTML; strip the escaped markup we just added.
    import re

    text = re.sub(r"<[^>]+>", "", result["answer_html"])
    return text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")


def main() -> None:
    import uvicorn

    setup_logging()
    uvicorn.run(app, host="127.0.0.1", port=_config().web_port)


if __name__ == "__main__":
    main()
