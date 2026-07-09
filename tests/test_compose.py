"""Anthropic-SDK answer composition: request shape, refusal handling, cost estimation."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from robin.agent import _compose_answer, _estimate_cost
from robin.config import RobinConfig
from robin.kb import Hit


class FakeMessages:
    def __init__(self, response) -> None:
        self._response = response
        self.last_request: dict | None = None

    def create(self, **kwargs):
        self.last_request = kwargs
        return self._response


def _response(stop_reason: str = "end_turn", text: str = "grounded answer"):
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=1_000,
            output_tokens=200,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeMessages:
    import anthropic

    messages = FakeMessages(_response())
    monkeypatch.setattr(
        anthropic, "Anthropic", lambda: SimpleNamespace(messages=messages)
    )
    return messages


def _config(tmp_path) -> RobinConfig:
    return RobinConfig(vault_path=tmp_path, repo_paths=[], var_dir=tmp_path / "var")


def test_compose_request_shape_and_result(tmp_path, fake: FakeMessages) -> None:
    sources = [Hit("arbiter/README.md", 3, "policy engine")]
    text, cost = _compose_answer("what is arbiter?", sources, _config(tmp_path))
    assert text == "grounded answer"
    request = fake.last_request
    assert request["model"] == "claude-opus-4-8"
    assert request["thinking"] == {"type": "adaptive"}
    assert "temperature" not in request and "top_p" not in request
    assert request["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "arbiter/README.md:3" in request["messages"][0]["content"]
    # opus-4-8 at $5/$25 per MTok: 1000 in + 200 out
    assert cost == pytest.approx(0.005 + 0.005)


def test_compose_handles_refusal(tmp_path, fake: FakeMessages) -> None:
    fake._response = _response(stop_reason="refusal", text="")
    fake._response.content = []
    text, cost = _compose_answer("q", [], _config(tmp_path))
    assert "can't help" in text
    assert cost is not None  # still logged for §7


def test_estimate_cost_includes_cache_tiers() -> None:
    usage = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=0,
        cache_creation_input_tokens=1_000_000,
        cache_read_input_tokens=1_000_000,
    )
    cost = _estimate_cost("claude-opus-4-8", usage)
    assert cost == pytest.approx(5.00 + 6.25 + 0.50)


def test_estimate_cost_unknown_model_is_none() -> None:
    usage = SimpleNamespace(input_tokens=10, output_tokens=10)
    assert _estimate_cost("some-future-model", usage) is None
