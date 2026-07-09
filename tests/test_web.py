"""Web adapter: auth (401/200/503), both endpoints with a stubbed pipeline and fake voice."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from robin.adapters import web
from robin.agent import Answer
from robin.config import RobinConfig
from robin.kb import Hit


class FakeStt:
    def transcribe(self, audio: bytes, mime: str) -> str:
        assert audio
        return "what is arbiter for?"


class FakeTts:
    def synthesize(self, text: str) -> bytes:
        return b"OggS-fake"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = RobinConfig(
        vault_path=tmp_path, repo_paths=[], var_dir=tmp_path / "var", web_token="sekret"
    )
    monkeypatch.setattr(web, "_config", lambda: config)
    monkeypatch.setattr(web, "_voice", lambda: (FakeStt(), FakeTts()))
    monkeypatch.setattr(
        web,
        "ask",
        lambda question, cfg, **kw: Answer(
            question=question,
            sources=[Hit("arbiter/README.md", 3, "policy engine")],
            text="Arbiter is the policy engine.",
            cost_usd=0.01,
        ),
    )
    return TestClient(web.app)


def _auth() -> dict:
    return {"Authorization": "Bearer sekret"}


def test_healthz_is_open(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"ok": True}


def test_ask_requires_token(client: TestClient) -> None:
    assert client.post("/api/ask", json={"text": "hi"}).status_code == 401
    bad = client.post(
        "/api/ask", json={"text": "hi"}, headers={"Authorization": "Bearer nope"}
    )
    assert bad.status_code == 401


def test_missing_server_token_is_503(client: TestClient, monkeypatch) -> None:
    config = web._config()
    monkeypatch.setattr(
        web, "_config",
        lambda: RobinConfig(vault_path=config.vault_path, repo_paths=[], web_token=None),
    )
    assert client.post("/api/ask", json={"text": "hi"}, headers=_auth()).status_code == 503


def test_ask_returns_rendered_answer(client: TestClient) -> None:
    response = client.post("/api/ask", json={"text": "arbiter?"}, headers=_auth())
    assert response.status_code == 200
    data = response.json()
    assert "policy engine" in data["answer_html"]
    assert data["sources"] == ["arbiter/README.md:3"]


def test_ask_voice_roundtrip(client: TestClient) -> None:
    response = client.post(
        "/api/ask-voice",
        files={"audio": ("voice.webm", b"fake-bytes", "audio/webm")},
        data={"chat_id": "t1"},
        headers=_auth(),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["transcript"] == "what is arbiter for?"
    assert "audio_b64" in data


def test_empty_audio_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/ask-voice",
        files={"audio": ("voice.webm", b"", "audio/webm")},
        headers=_auth(),
    )
    assert response.status_code == 400
