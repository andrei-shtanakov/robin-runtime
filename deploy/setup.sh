#!/usr/bin/env bash
# One-time VPS bring-up for Robin (Ubuntu/Debian). Idempotent. Run as root from the
# robin-runtime checkout: sudo deploy/setup.sh
set -euo pipefail

ROBIN_HOME=/srv/robin
RUNTIME="$ROBIN_HOME/robin-runtime"
MIRRORS="$ROBIN_HOME/mirrors"
UNIT_DIR=/etc/systemd/system

# Mirror remotes: read-only view of the ecosystem (slot 6). Adjust to your remotes —
# see projects.md in the workspace root for the canonical list.
GIT_BASE="${GIT_BASE:?set GIT_BASE, e.g. git@github.com:your-org}"
REPOS=(prograph-vault atp-platform Maestro arbiter spec-runner deployer dispatcher steward robin-runtime)

echo "== packages =="
apt-get update -q
apt-get install -y -q git curl nginx
# system-wide so the robin user can run it too; slot 2 is the direct anthropic SDK
# (pure Python) — no Node/Claude CLI needed.
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

echo "== user + layout =="
id robin &>/dev/null || useradd --system --create-home --home-dir "$ROBIN_HOME" robin
mkdir -p "$MIRRORS" "$RUNTIME" "$ROBIN_HOME/var"
chown -R robin:robin "$ROBIN_HOME"

echo "== runtime code =="
if [ ! -d "$RUNTIME/.git" ]; then
    sudo -u robin git clone "$GIT_BASE/robin-runtime.git" "$RUNTIME"
fi
(cd "$RUNTIME" && sudo -u robin uv sync)

echo "== mirrors (read-only) =="
for repo in "${REPOS[@]}"; do
    [ -d "$MIRRORS/$repo/.git" ] || sudo -u robin git clone "$GIT_BASE/$repo.git" "$MIRRORS/$repo"
done

echo "== env =="
if [ ! -f "$ROBIN_HOME/robin.env" ]; then
    cp "$RUNTIME/deploy/env.example" "$ROBIN_HOME/robin.env"
    chmod 600 "$ROBIN_HOME/robin.env" && chown robin:robin "$ROBIN_HOME/robin.env"
    echo ">>> EDIT $ROBIN_HOME/robin.env (tokens, channel ids, TZ) before starting units"
fi

echo "== systemd units =="
cp "$RUNTIME"/deploy/systemd/*.service "$RUNTIME"/deploy/systemd/*.timer "$UNIT_DIR"/
systemctl daemon-reload
systemctl enable --now robin-mirror-sync.timer robin-digest-daily.timer \
    robin-digest-weekly.timer robin-liveness.timer
echo ">>> after editing robin.env: systemctl enable --now robin-telegram robin-web"
echo ">>> nginx: see deploy/README.md for the TLS reverse-proxy block"
