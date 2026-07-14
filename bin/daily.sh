#!/bin/sh
# Daily Oracle Duel cycle — invoked by launchd at 7am (with catch-up on wake).
# Idempotent: exits quietly if today's cycle already ran, so a catch-up firing
# or a manual run can never double-spend.
# Resolve the repo root even when invoked via symlink; refuse to run elsewhere.
SELF="$(readlink -f "$0" 2>/dev/null || echo "$0")"
cd "$(dirname "$SELF")/.." || exit 1
[ -f engine/orchestrator.py ] || { echo "daily.sh: not at repo root: $PWD" >&2; exit 1; }
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
# Exit 75 = transient skip (network not up yet at boot, exchange closed or
# unreachable, empty scan). Retry a few times and NEVER stamp the day for a
# skip — only a genuinely completed cycle may claim the date.
attempt=0
while :; do
    .venv/bin/python -m engine.orchestrator cycle >> logs/cron.log 2>&1
    rc=$?
    if [ "$rc" -eq 0 ]; then
        echo "$TODAY" > state/.last_cycle
        break
    fi
    [ "$rc" -ne 75 ] && exit "$rc"
    attempt=$((attempt+1))
    [ "$attempt" -ge 5 ] && exit 75
    sleep 180
done
