# Oracle Duel — handoff (2026-07-10)

Read `CLAUDE.md` first: it has the experiment design, ground rules, phase plan,
and the pre-registered go/no-go gate. This file is the point-in-time state as of
the evening of 2026-07-10 (day 6 of Phase 0.5 paper trading).

## Hard rules for any assistant working here

- `engine/risk.py` is the immutable risk layer, kept chmod 444. Do NOT edit it
  without the owner's explicit sign-off in the conversation; re-lock to 444
  after any approved edit and add a dated note to CLAUDE.md.
- Never re-run a day's `cycle` — it spends real inference money and would
  double-forecast. `bin/daily.sh` guards via `state/.last_cycle`; don't bypass.
- Do not tune the gate thresholds in `engine/gates.py` or the metrics — they
  are pre-registered; changes invalidate the experiment.
- Sampling-frame changes (scanner filters/caps) require an owner-approved dated
  note in CLAUDE.md (two exist: 2026-07-08, 2026-07-10).
- Paper mode (`live: false` in config.yaml) is on. `state/STOP` kills orders.
- Both agents must stay symmetric: same toolbox, markets, harness prompts.
  Agent-specific logic belongs ONLY in `agents/<name>/prompt.md`.

## Current state (evening 2026-07-10)

- Phase 0.5 (paper). Bankrolls: claude $55.40 cash + ~$32.60 riding (9 open
  bets, Trump bet settled −$3.82 this evening); gpt $60.52 cash + ~$34.17
  riding (9 open). Every settled bet so far has lost: claude −$10.17, gpt −$3.47.
- Brier: claude 0.1295 (n=25+), gpt 0.1094 (n=34+); n grew by ~14 in the
  evening settle pass. Paired Δbrier vs market is NEGATIVE for both (positive =
  agent beats market): claude −0.0087 [−0.0207, +0.0033], gpt −0.0208
  [−0.0570, +0.0154]. Early lean is toward KILL, but n << 300 so it's noise.
- Gate engages at n=300 resolved per agent, ETA ~Jul 20–24 at the current
  ~20-25 resolutions/day. Check with: `.venv/bin/python -m engine.orchestrator gate`.
- First prompt-evolution round closes ~Jul 13 (needs 7 days on v1 AND 100
  resolved per agent). Both agents still on prompt v1; postmortems table empty.
- Notable open position: gpt bet NO on BOTH sides of a rained-out KBO game
  (KXKBOGAME-26JUL090530KIWKTW) — resolution depends on whether Kalshi waits
  for the makeup game. Watch how it settles.
- Inference spend: claude ~$2.4/day (sonnet-5), gpt ~$5/day (gpt-5.5).
  Cumulative ~$37 total. Claude's telemetry was reset 2026-07-06 at the
  opus→sonnet switch; gpt history kept (hence the n gap).

## Changes shipped 2026-07-10 (all committed, tests green: 38 passed)

1. `c40737e` — self-match guard in risk.py now fires only when `live: true`.
   In paper it censored the second agent's bet whenever the first took the
   opposite side (only ever hit gpt). Owner signed off on the risk.py edit.
2. `7bdae0f` — daily agent run order rotates by date parity (odd ordinal =
   reversed). Before, claude always went first, so shared-quota outages (7/9
   Brave exhaustion, 99 failed gpt searches) always hit gpt.
3. `2d771e3` — new `orchestrator settle` command + `bin/settle.sh`, run by a
   second launchd job `com.oracleduel.settle` at 19:00 local (the daily cycle
   is `com.oracleduel.daily` at 07:00 via `bin/daily.sh`). Settles same-day
   closes the same evening; idempotent and safe to run manually.
4. `aedb593` — scanner `max_per_series: 3` (config.yaml): price ladders relist
   as a fresh event per close time, so SOL-today + SOL-tomorrow strikes stacked
   past `max_per_event: 2`. Series key = `series_ticker` or event-ticker prefix.
5. `d1dfb34` — CLAUDE.md dated note covering the above.

## Known items deliberately NOT done

- gpt's near-blind 7/9 forecasts stay in the gate sample (no post-hoc data
  exclusion; the DuckDuckGo fallback prevents recurrence).
- Batch-API cost cuts and gpt→5.6-Terra model swap: deferred until the gate
  verdict (symmetry / attribution).
- Brave search is capped at $10/mo; bump around Jul 17 if the gate drags.
- Sonnet-5 intro pricing in config expires 2026-08-31.
- Phase 4 (calibration meta-model) is spec'd in CLAUDE.md; do not start until
  Phase 2 is stable.

## Operational map

- Daily flow: launchd 07:00 → `bin/daily.sh` → `orchestrator cycle` (settle →
  scan → forecast both agents → risk-engine orders → round close if due →
  digest). Evening: launchd 19:00 → `orchestrator settle`.
- Env: secrets in `.env` (sourced by the shell scripts); Python is
  `.venv/bin/python` (bare `python` lacks deps).
- Data: `state/telemetry.db` (sqlite; tables: forecasts, trades, agent_state,
  prompt_versions, postmortems, incidents, token_spend, tool_health).
  `state/dashboard.html` via `orchestrator dashboard`. Logs in `logs/`.
- Kalshi: public v2 API, dollar-string fields (`yes_bid_dollars`, `volume_fp`);
  category on the event object; `mve_collection_ticker` marks excluded parlays.
  Markets finalize with a lag after close — a market can be past close_time
  and still `active`.
- Tests: `.venv/bin/python -m pytest` (38 tests).
