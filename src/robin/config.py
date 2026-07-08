"""Robin runtime configuration (ROBIN-SPEC slots 5, 6, 7, 15, 17).

Minimal stdlib config for the M0 slice. Production SHOULD move to pydantic-settings (repo
convention) once dependencies are installed; kept dependency-free here so the M0 tool layer
runs and tests pass without an environment. Secrets come from env, never the KB (slot 17).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# robin-runtime/ is a sibling of the knowledge repo and the ecosystem repos.
_ECOSYSTEM = Path(__file__).resolve().parents[3]  # .../all_ai_orchestrators
_DEFAULT_VAULT = _ECOSYSTEM / "prograph-vault"
_ECOSYSTEM_REPOS = (
    "atp-platform", "Maestro", "arbiter", "spec-runner", "deployer", "dispatcher", "steward",
)


@dataclass(frozen=True)
class RobinConfig:
    """Resolved runtime configuration."""

    vault_path: Path
    repo_paths: list[Path]
    model: str = "claude-opus-4-8"
    daily_budget_usd: float = 5.0  # §7 cost cap
    allowed_dm_users: tuple[str, ...] = ()  # slot 7: chat identity registry

    def read_roots(self) -> list[Path]:
        """All read-only roots the tool layer may search (§4)."""
        return [self.vault_path, *self.repo_paths]


def load_config() -> RobinConfig:
    """Build config from env with ecosystem defaults."""
    vault = Path(os.environ.get("ROBIN_VAULT", str(_DEFAULT_VAULT))).resolve()
    base = vault.parent
    repos = [base / name for name in _ECOSYSTEM_REPOS if (base / name).is_dir()]
    users = tuple(u for u in os.environ.get("ROBIN_ALLOWED_DM", "").split(",") if u)
    budget = float(os.environ.get("ROBIN_DAILY_BUDGET_USD", "5"))
    return RobinConfig(
        vault_path=vault,
        repo_paths=repos,
        allowed_dm_users=users,
        daily_budget_usd=budget,
    )
