#!/bin/sh
# Evening settle-only pass — invoked by launchd at 7pm. Grades forecasts and
# settles trades that finalized after the morning cycle, so same-day bets hit
# the wallet the same evening. Read-mostly and idempotent: score_resolutions
# only touches still-unresolved rows, so extra runs are harmless.
# Resolve the repo root even when invoked via symlink; refuse to run elsewhere.
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
cd "$(dirname "$SELF")/.." || exit 1
[ -f engine/orchestrator.py ] || { echo "settle.sh: not at repo root: $PWD" >&2; exit 1; }
set -a
. ./.env
set +a
.venv/bin/python -m engine.orchestrator settle >> logs/cron.log 2>&1
