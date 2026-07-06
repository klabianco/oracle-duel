#!/bin/sh
# Daily Oracle Duel cycle — invoked by cron. Absolute paths only; cron has no env.
cd /Users/kevinl/projects/bet || exit 1
set -a
. ./.env
set +a
exec .venv/bin/python -m engine.orchestrator cycle >> logs/cron.log 2>&1
