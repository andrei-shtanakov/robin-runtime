"""Grounded-answer entrypoint (ROBIN-SPEC M0/M1) + append-only interaction log (§7).

M0: retrieve grounding for a real question FROM the knowledge repo. M1: compose a cited answer
with the Claude Agent SDK, using ranked authoritative sources (finding #8) and an isolated
workspace (§6.5). Without a key/SDK, ``ask(retrieve_only=True)`` still returns cited sources.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .config import RobinConfig, load_config
from .kb import Hit, Kb

_LOG_DIR = Path(__file__).resolve().parents[2] / "var"

_ANSWER_RULES = (
    "Answer the teammate's question using ONLY the SOURCES below. "
    "Cite the source for every claim as `path:line`. Answer in the asker's language. "
    "If the SOURCES do not contain the answer, say plainly it is not in the knowledge "
    "repo — do not guess."
)


@dataclass
class Answer:
    """A grounded answer plus the sources it must cite."""

    question: str
    sources: list[Hit]
    text: str | None  # None when retrieve_only or the SDK is not wired
    cost_usd: float | None = None


def ask(
    question: str,
    config: RobinConfig | None = None,
    *,
    retrieve_only: bool = False,
) -> Answer:
    """Retrieve ranked grounding and (optionally) compose a cited answer."""
    config = config or load_config()
    kb = Kb(config.read_roots())
    sources = kb.grounding_hits(question, max_hits=12)
    text: str | None = None
    cost: float | None = None
    if not retrieve_only:
        text, cost = _compose_answer(question, sources, config)
    _log(surface="cli", question=question, n_sources=len(sources), cost_usd=cost)
    return Answer(question=question, sources=sources, text=text, cost_usd=cost)


def build_prompt(question: str, sources: list[Hit]) -> str:
    """Assemble the grounded user prompt from ranked sources (testable without the SDK)."""
    lines = ["SOURCES:"]
    lines += [f"- {hit.path}:{hit.line}: {hit.text}" for hit in sources]
    lines += ["", f"QUESTION: {question}"]
    return "\n".join(lines)


def _system_prompt(config: RobinConfig) -> str:
    soul = config.vault_path / "soul.md"
    persona = soul.read_text(errors="ignore") if soul.is_file() else ""
    return f"{persona}\n\n---\n{_ANSWER_RULES}".strip()


def _compose_answer(
    question: str, sources: list[Hit], config: RobinConfig
) -> tuple[str, float | None]:
    try:
        import anyio
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            TextBlock,
            query,
        )
    except ImportError as exc:  # pragma: no cover - needs the SDK installed
        raise RuntimeError(
            "claude-agent-sdk not installed. Run `uv add claude-agent-sdk` and set "
            "ANTHROPIC_API_KEY, or call ask(retrieve_only=True) for the M0 slice."
        ) from exc

    prompt = build_prompt(question, sources)
    # §6.5 isolation: setting_sources left unset => host CLAUDE.md / .mcp.json / settings are
    # NOT loaded into Robin's context. No tools: the orchestrator already did retrieval (§3).
    options = ClaudeAgentOptions(system_prompt=_system_prompt(config), max_turns=1)

    async def _run() -> tuple[str, float | None]:
        chunks: list[str] = []
        cost: float | None = None
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                chunks += [b.text for b in message.content if isinstance(b, TextBlock)]
            elif isinstance(message, ResultMessage):
                cost = message.total_cost_usd
        return "".join(chunks), cost

    return anyio.run(_run)


def _log(**fields: object) -> None:
    # §7: append-only interaction log in Robin's own store, never the KB.
    _LOG_DIR.mkdir(exist_ok=True)
    record = {"ts": int(time.time()), **fields}
    with (_LOG_DIR / "interactions.jsonl").open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def _main() -> None:
    import os
    import sys

    question = " ".join(sys.argv[1:]) or "Which repo owns the agents-catalog SSOT?"
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    answer = ask(question, retrieve_only=not has_key)
    print(f"Q: {answer.question}")
    if answer.text is not None:
        print(f"\nA: {answer.text}\n")
        if answer.cost_usd is not None:
            print(f"(cost ${answer.cost_usd:.4f})")
    else:
        print("(retrieve-only: no ANTHROPIC_API_KEY — showing grounding sources)")
    print(f"Grounding sources ({len(answer.sources)}):")
    for hit in answer.sources:
        print(f"  {hit.path}:{hit.line}: {hit.text}")


if __name__ == "__main__":
    _main()
