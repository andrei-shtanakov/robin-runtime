# robin-runtime

Implementation home for **Robin**, the AI chief of staff for the AI-Orchestrators ecosystem.
Governed by [`../prograph-vault/ROBIN-SPEC.local.md`](../prograph-vault/ROBIN-SPEC.local.md);
identity is `../prograph-vault/soul.md`, duties are `../prograph-vault/robin/duties.md`.

## Topology (ROBIN-SPEC §3)

Robin runs in **its own** directory. The knowledge repo and ecosystem repos are mounted
**read-only** as siblings; Robin never writes them, and never reads `_cowork_output/`.

```
all_ai_orchestrators/
├── robin-runtime/     ← this repo (code + Robin's own store)
├── prograph-vault/    ← knowledge repo (read-only mount)
├── Maestro/ arbiter/ atp-platform/ …   ← ecosystem repos (read-only mounts)
└── _cowork_output/    ← dev-scratch — NEVER read at runtime
```

## Layout

| Path | Role (spec) |
|---|---|
| `src/robin/config.py` | mounts, model, budget, chat registry (slots 5,6,7,17; §7) |
| `src/robin/kb.py` | read-only Tool Layer — search/read over the mounts (§3, §4) |
| `src/robin/agent.py` | grounded-answer entrypoint (M0/M1) + append-only log (§7) |
| `tests/test_kb.py` | M0 tool-layer test — finds the SSOT answer, skips `_cowork_output` |
| `var/` | Robin's own store (interaction log, later SQLite) — gitignored |

## Run the M0 slice (no API key)

```bash
# retrieve grounding for a real question straight from the vault:
PYTHONPATH=src python3 -m robin.agent "Which repo owns the agents-catalog SSOT?"
# raw KB search:
PYTHONPATH=src python3 -m robin.kb "agents-catalog.toml"
# tests:
uv run pytest          # or: PYTHONPATH=src python3 -m pytest
```

`agent.ask(..., retrieve_only=True)` returns cited sources without the model. Wiring the
Claude Agent SDK in `_compose_answer` (in an isolated workspace, §6.5) completes M0→M1.

## What's next (per ../prograph-vault/robin/duties.md + IMPLEMENTATION-PLAN)

1. **M0→M1:** wire `_compose_answer` to the Claude Agent SDK (isolated workspace; soul.md in
   the system prompt; cite source; answer in asker's language; escape output §6.7).
2. **Chat adapter:** Telegram long-polling → DM/mention → same pipeline (slot 1).
3. **Onboarding (Phase 3):** load `../prograph-vault/authored/skills/onboarding/`, implement
   `progress-schema.sql` in `var/robin.db`.
4. **Hardening (mandatory, ROBIN-SPEC.local.md Appendix):** read-back verification (§6.4),
   liveness + cost cap + negative-evidence (§7), context isolation (§6.5).

> Log every clarifying question in `../prograph-vault/QUESTIONS.md` — each is a spec bug for
> upstreaming; do not patch the local spec to answer it.
