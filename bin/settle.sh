#!/bin/sh
# Evening settle-only pass — invoked by launchd at 7pm. Grades forecasts and
# settles trades that finalized after the morning cycle, so same-day bets hit
# the wallet the same evening. Read-mostly and idempotent: score_resolutions
# only touches still-unresolved rows, so extra runs are harmless.
cd "$(dirname "$0")/.." || exit 1
set -a
. ./.env
set +a
.venv/bin/python -m engine.orchestrator settle >> logs/cron.log 2>&1
