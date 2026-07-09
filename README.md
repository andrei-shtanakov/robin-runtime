# robin-runtime

Implementation home for **Robin**, the AI chief of staff for the AI-Orchestrators ecosystem.
Governed by [`../prograph-vault/ROBIN-SPEC.local.md`](../prograph-vault/ROBIN-SPEC.local.md);
identity is `../prograph-vault/soul.md`, duties are `../prograph-vault/robin/duties.md`.

Robin answers teammate questions — in **text and voice** — grounded ONLY in the knowledge
repo and the read-only ecosystem mirrors, always with `path:line` citations: what a repo or
module is for, what changed today/this week/any period (git history), what's planned.
Surfaces: Telegram (DM + group mentions + digest channel) and a token-gated web chat page.

## Topology (ROBIN-SPEC §3)

Robin runs in **its own** directory. The knowledge repo and ecosystem repos are mounted
**read-only** as siblings; Robin never writes them, and never reads `_cowork_output/`.

```
all_ai_orchestrators/          (VPS: /srv/robin/ + mirrors/)
├── robin-runtime/     ← this repo (code + Robin's own store in var/)
├── prograph-vault/    ← knowledge repo (read-only mount)
├── Maestro/ arbiter/ atp-platform/ …   ← ecosystem repos (read-only mounts)
└── _cowork_output/    ← dev-scratch — NEVER read at runtime
```

## Layout

| Path | Role (spec) |
|---|---|
| `src/robin/config.py` | env config: mounts, model, §7 caps, chat registry (slots 5,6,7,17) |
| `src/robin/kb.py` | read-only Tool Layer — search/read over the mounts (§3, §4) |
| `src/robin/agent.py` | the single LLM call site: routed retrieval → grounded cited answer + append-only log (§7) |
| `src/robin/changes.py` | "what changed?" retrieval: period parsing (RU/EN) + `git log` over mirrors + vault journals |
| `src/robin/digest.py` | daily/weekly digest duty → Telegram channel + `var/digests/` (M2) |
| `src/robin/guard.py` | §7 daily budget cap, per-user rate limit, `/cost` report |
| `src/robin/memory.py` | per-chat rolling window, last N turns (slots 12–14) |
| `src/robin/voice.py` | STT/TTS behind protocols; OpenAI default (slot 21: transcribe first) |
| `src/robin/fmt.py` | §6.7 HTML escaping + 4096 chunking for Telegram |
| `src/robin/liveness.py` | §7 alert when the newest digest is older than cadence + grace |
| `src/robin/adapters/telegram.py` | long-polling bot: DM/mentions, voice notes, `/digest`, `/cost` |
| `src/robin/adapters/web.py` | FastAPI: `/api/ask`, `/api/ask-voice`, single-page chat with mic |
| `deploy/` | VPS bring-up: setup.sh, mirror sync, systemd services + timers, README |
| `var/` | Robin's own store (interactions, digests, chat windows) — gitignored |

## Run locally

```bash
uv sync
uv run pytest                                  # offline suite (no keys needed)
uv run python -m robin.agent "Which repo owns the agents-catalog SSOT?"
#   without ANTHROPIC_API_KEY: prints grounding sources (retrieve-only M0 slice)
#   with the key: composed, cited answer + cost
uv run python -m robin.changes "что изменилось за неделю?"   # raw change retrieval
uv run python -m robin.adapters.telegram       # needs TELEGRAM_BOT_TOKEN (+ OPENAI_API_KEY for voice)
uv run python -m robin.adapters.web            # needs ROBIN_WEB_TOKEN; serves 127.0.0.1:8080
```

Secrets: copy `.env.example` → `.env` and export (slot 17 — env only, never the KB).

## Deploy

`deploy/README.md` covers the full VPS bring-up: mirrors + sync timer, digest/liveness
timers, nginx TLS, BotFather setup, and the manual smoke checklist (M1/M2 acceptance).

> Log every clarifying question in `../prograph-vault/QUESTIONS.md` — each is a spec bug for
> upstreaming; do not patch the local spec to answer it.
