#!/bin/sh
# Daily Oracle Duel cycle — invoked by launchd at 7am (with catch-up on wake).
# Idempotent: exits quietly if today's cycle already ran, so a catch-up firing
# or a manual run can never double-spend.
cd /Users/kevinl/projects/bet || exit 1
TODAY=$(date +%Y-%m-%d)
if [ -f state/.last_cycle ] && [ "$(cat state/.last_cycle)" = "$TODAY" ]; then
    exit 0
fi
LOCK="state/.cycle-${TODAY}.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT HUP INT TERM
set -a
. ./.env
set +a
export ORACLE_CYCLE_DATE="$TODAY"
.venv/bin/python -m engine.orchestrator cycle >> logs/cron.log 2>&1 \
    && echo "$TODAY" > state/.last_cycle
