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
# Canonical repo names, which are also the mirror directory names: a plain `git clone`
# names the directory after the repo, so anything else here goes silently blind on every
# checkout that was cloned that way. `maestro` and `libretto` were renamed 2026-07-16 and
# this list kept the old names until 2026-07-26: both were absent from the digest on every
# teammate's machine, while GitHub's redirect kept the VPS mirrors working under their dead
# names. Names here are paths, so they stay lowercase even where the product brand is not
# (`maestro` the directory, Maestro the orchestrator). Keep in sync with deploy/setup.sh
# (test_config asserts it) and prefer canonical names over whatever a redirect resolves.
_ECOSYSTEM_REPOS = (
    "atp-platform",
    "maestro",
    "arbiter",
    "spec-runner",
    "spec-runner-vscode",
    "deployer",
    "dispatcher",
    "steward",
    "proctor",
    "prograph",
    "discovery",
    "libretto",
    "research-bench",
    "github-checker",
    # product-governance contracts + validator (Idea -> RankedBacklog ->
    # ProductProposal -> human gates); registered at bootstrap 2026-08-12 so the
    # digest is not silently blind to a new repo (the maestro/libretto lesson).
    "impresario",
    # Robin's own repo: without it "what changed?" answers are blind to Robin's own
    # development. Like every entry here it resolves to a sibling of ROBIN_VAULT — on
    # the VPS mirror layout that is a separate read-only clone (the runtime checkout
    # stays out of retrieval); on a local workspace it is the live working copy.
    "robin-runtime",
)

# Canonical plan-exemption registry (PP-101 stage 6, owner ruling 2026-08-12).
# Lives in code next to _ECOSYSTEM_REPOS so every Robin installation reads the
# same governance coverage — the env-only ROBIN_PLAN_EXEMPT let two deployments
# disagree about it silently and is gone. Empty today on purpose: prograph-vault
# was exempted 2026-07-26 and un-exempted 2026-07-30, because its plan file is
# the acceptance surface for cross-repo obligations (ADR-ECO-006) — its
# disappearance must raise a normal coverage warning, not vanish from the
# denominator. Adding the first entry requires a repo decision with a recorded
# motivation and tests, not a config tweak.
_PLAN_EXEMPT: tuple[str, ...] = ()


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
    # Open plan items carried into the weekly digest. Sized above the ecosystem's real
    # open-item count (62 across 12 repos on 2026-07-26) so the "plan picture is
    # incomplete" disclaimer reflects a genuine overflow, not a too-tight budget.
    plan_items_max: int = 80
    # Mirrors (by directory name) that are not expected to keep a plan file. They
    # leave the coverage denominator instead of being reported as a gap every run;
    # applied and unknown exemptions are ALWAYS disclosed in the digest, including
    # at full coverage. Exempt means "not expected to keep one", not "ignore the
    # one it has": if such a repo does carry a plan file, its items still reach
    # the digest. The canonical registry is _PLAN_EXEMPT (in-repo, not env);
    # the dataclass default stays neutral for directly-constructed test configs,
    # load_config() applies the canon — same pattern as freshness_repo below.
    plan_exempt: tuple[str, ...] = ()
    # Mirror names from _ECOSYSTEM_REPOS that did not resolve to a directory. A
    # name that stopped resolving used to vanish from the digest silently (the
    # maestro/libretto blindness, 2026-07); coverage now reports these loudly
    # (PP-101 stage 6: fail-loud on unresolved names).
    missing_mirrors: tuple[str, ...] = ()

    # Independent reader of steward's arch-evidence-freshness cron workflow
    # (issue #42): `owner/repo` whose last run each digest reads via the public
    # GitHub API. Empty = reader off. The dataclass default is off so directly-
    # constructed test configs never touch the network; load_config() turns it on.
    freshness_repo: str = ""

    # slot 1: Telegram surface. Secrets env-only (slot 17); non-secrets kept here too so
    # every adapter reads one object.
    telegram_token: str | None = None
    telegram_channel: str | None = None  # digest destination (@channel or -100… id)
    maintainer_chat: str | None = None  # liveness alerts land here (DM chat id)

    # web surface
    web_token: str | None = None
    web_port: int = 8080

    # slot 21 voice: provider-keyed (voice.py registry). Models are env-pinnable — use an
    # alias or a dated snapshot, but only ids the OpenAI project actually exposes
    # (GET /v1/models); a non-enabled model 403s at call time.
    stt_provider: str = "openai"
    tts_provider: str = "openai"
    stt_model: str = "whisper-1"
    tts_model: str = "tts-1"
    tts_voice: str = "alloy"

    history_turns: int = 10  # slots 13/14: reseed window

    # slot 8: passive-capture scope — group chats whose messages Robin keeps as ambient
    # context for @mentions (§6.2). MUST be disclosed to the team (§6.5); empty = off.
    capture_chats: tuple[str, ...] = ()
    ambient_messages: int = 10  # slot 13: ambient context size N

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
    missing = tuple(name for name in _ECOSYSTEM_REPOS if not (base / name).is_dir())
    users = tuple(
        u.strip()
        for u in os.environ.get("ROBIN_ALLOWED_DM", "").split(",")
        if u.strip()
    )
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
        plan_items_max=int(os.environ.get("ROBIN_PLAN_ITEMS_MAX", "80")),
        plan_exempt=_PLAN_EXEMPT,
        missing_mirrors=missing,
        freshness_repo=os.environ.get(
            "ROBIN_FRESHNESS_REPO", "andrei-shtanakov/steward"
        ).strip(),
        telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_channel=os.environ.get("ROBIN_TELEGRAM_CHANNEL") or None,
        maintainer_chat=os.environ.get("ROBIN_MAINTAINER_CHAT") or None,
        web_token=os.environ.get("ROBIN_WEB_TOKEN") or None,
        web_port=int(os.environ.get("ROBIN_WEB_PORT", "8080")),
        stt_provider=os.environ.get("ROBIN_STT_PROVIDER", "openai"),
        tts_provider=os.environ.get("ROBIN_TTS_PROVIDER", "openai"),
        stt_model=os.environ.get("ROBIN_STT_MODEL", "whisper-1"),
        tts_model=os.environ.get("ROBIN_TTS_MODEL", "tts-1"),
        tts_voice=os.environ.get("ROBIN_TTS_VOICE", "alloy"),
        history_turns=int(os.environ.get("ROBIN_HISTORY_TURNS", "10")),
        capture_chats=tuple(
            c.strip()
            for c in os.environ.get("ROBIN_CAPTURE_CHATS", "").split(",")
            if c.strip()
        ),
        ambient_messages=int(os.environ.get("ROBIN_AMBIENT_N", "10")),
    )
