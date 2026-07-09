"""Robin runtime configuration (ROBIN-SPEC slots 5, 6, 7, 15, 17).

Minimal stdlib config. Secrets come from env, never the KB (slot 17). Deployment layout
(local workspace or VPS mirrors) is selected purely via ROBIN_VAULT: ecosystem repos are
discovered as siblings of the vault, so `/srv/robin/mirrors/prograph-vault` just works.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# robin-runtime/ is a sibling of the knowledge repo and the ecosystem repos.
_ECOSYSTEM = Path(__file__).resolve().parents[3]  # .../all_ai_orchestrators
_DEFAULT_VAULT = _ECOSYSTEM / "prograph-vault"
_DEFAULT_VAR = Path(__file__).resolve().parents[2] / "var"
_ECOSYSTEM_REPOS = (
    "atp-platform", "Maestro", "arbiter", "spec-runner", "deployer", "dispatcher", "steward",
)


@dataclass(frozen=True)
class RobinConfig:
    """Resolved runtime configuration."""

    vault_path: Path
    repo_paths: list[Path]
    var_dir: Path = _DEFAULT_VAR
    model: str = "claude-opus-4-8"
    daily_budget_usd: float = 5.0  # §7 cost cap
    user_daily_msgs: int = 30  # §7 per-user rate limit
    allowed_dm_users: tuple[str, ...] = ()  # slot 7: chat identity registry
    tz: str = "UTC"  # duties.md <TZ>; drives "today" parsing and digest windows
    digest_grace_hours: int = 6  # §7 liveness: cadence + grace

    # slot 1: Telegram surface. Secrets env-only (slot 17); non-secrets kept here too so
    # every adapter reads one object.
    telegram_token: str | None = None
    telegram_channel: str | None = None  # digest destination (@channel or -100… id)
    maintainer_chat: str | None = None  # liveness alerts land here (DM chat id)

    # web surface
    web_token: str | None = None
    web_port: int = 8080

    # slot 21 voice: provider-keyed (voice.py registry)
    stt_provider: str = "openai"
    tts_provider: str = "openai"
    tts_voice: str = "alloy"

    history_turns: int = 10  # slots 13/14: reseed window

    def read_roots(self) -> list[Path]:
        """All read-only roots the tool layer may search (§4). Robin's own digests are
        included so "what did I miss?" can cite them (M2) — they live in var/, not the KB."""
        roots = [self.vault_path, *self.repo_paths]
        digests = self.var_dir / "digests"
        if digests.is_dir():
            roots.append(digests)
        return roots


def load_config() -> RobinConfig:
    """Build config from env with ecosystem defaults."""
    vault = Path(os.environ.get("ROBIN_VAULT", str(_DEFAULT_VAULT))).resolve()
    base = vault.parent
    repos = [base / name for name in _ECOSYSTEM_REPOS if (base / name).is_dir()]
    users = tuple(u.strip() for u in os.environ.get("ROBIN_ALLOWED_DM", "").split(",") if u.strip())
    return RobinConfig(
        vault_path=vault,
        repo_paths=repos,
        var_dir=Path(os.environ.get("ROBIN_VAR_DIR", str(_DEFAULT_VAR))).resolve(),
        model=os.environ.get("ROBIN_MODEL", "claude-opus-4-8"),
        daily_budget_usd=float(os.environ.get("ROBIN_DAILY_BUDGET_USD", "5")),
        user_daily_msgs=int(os.environ.get("ROBIN_USER_DAILY_MSGS", "30")),
        allowed_dm_users=users,
        tz=os.environ.get("ROBIN_TZ", "UTC"),
        digest_grace_hours=int(os.environ.get("ROBIN_DIGEST_GRACE_HOURS", "6")),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_channel=os.environ.get("ROBIN_TELEGRAM_CHANNEL") or None,
        maintainer_chat=os.environ.get("ROBIN_MAINTAINER_CHAT") or None,
        web_token=os.environ.get("ROBIN_WEB_TOKEN") or None,
        web_port=int(os.environ.get("ROBIN_WEB_PORT", "8080")),
        stt_provider=os.environ.get("ROBIN_STT_PROVIDER", "openai"),
        tts_provider=os.environ.get("ROBIN_TTS_PROVIDER", "openai"),
        tts_voice=os.environ.get("ROBIN_TTS_VOICE", "alloy"),
        history_turns=int(os.environ.get("ROBIN_HISTORY_TURNS", "10")),
    )
