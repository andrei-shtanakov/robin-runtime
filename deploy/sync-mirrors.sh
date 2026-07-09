#!/usr/bin/env bash
# Fast-forward every read-only mirror under $MIRRORS. Run by robin-mirror-sync.timer.
# Mirrors are Robin's ONLY view of the ecosystem on the VPS — never edited, only pulled.
set -u
MIRRORS="${MIRRORS:-/srv/robin/mirrors}"

status=0
for repo in "$MIRRORS"/*/; do
    [ -d "$repo/.git" ] || continue
    if ! git -C "$repo" pull --ff-only --quiet; then
        echo "sync failed: $repo" >&2
        status=1
    fi
done
exit $status
