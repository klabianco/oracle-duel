# Oracle Duel — Self-Improving Forecasting Agents on Kalshi

Two AI agents (Claude vs GPT) compete as forecasters on real Kalshi prediction markets.
Each maintains a 500-word strategy prompt it rewrites after every graded round.
Learning signal comes from **forecasts** (all Brier-scored); only the top 2–3 edges become
small real-money bets, sized by an immutable risk engine.

> **Disclaimer:** This is a research experiment in LLM forecasting and calibration, not
> betting advice or a trading product. It defaults to paper mode (`live: false`) and its
> pre-registered go/no-go gate (`engine/gates.py`, locked 2026-07-05 — see CLAUDE.md) exists
> precisely because the null hypothesis is that this does *not* beat the market. Nothing
> here is financial advice; if you run it with `live: true`, that's on you.
>
> **A note on "immutable":** `engine/risk.py` being immutable is a *local runtime
> convention* (chmod 444 + the agents having no write path to it), enforced in this
> deployment. A fork can of course change anything; forks' behavior says nothing about
> this experiment.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in keys, then: set -a; source .env; set +a
pytest                 # unit tests
```

## Phases

**Phase 0 — mock (pipeline debugging, zero spend):**
```bash
ORACLE_MOCK=1 python -m engine.orchestrator cycle
ORACLE_MOCK=1 python -m engine.orchestrator fast-forward 7   # advance mock clock
ORACLE_MOCK=1 python -m engine.orchestrator cycle            # resolves + scores + may close a round
```

**Phase 0.5 — paper (real markets, real LLMs, no orders):** default config (`live: false`).
```bash
python -m engine.orchestrator cycle
```

**Phase 1+ — live:** set `live: true` in config.yaml (or `ORACLE_LIVE=1`). Orders are
limit orders at the evaluated ask, capped by the risk engine.

## Scheduling

**macOS (launchd — what this repo uses):** two jobs, a 7am forecast cycle and a
7pm settle-only pass. Templates are in `bin/`; replace `/path/to/bet` with your
checkout path, then:

```bash
sed -i '' "s|/path/to/bet|$PWD|g" bin/com.oracleduel.daily.plist bin/com.oracleduel.settle.plist
cp bin/com.oracleduel.*.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.oracleduel.daily.plist
launchctl load ~/Library/LaunchAgents/com.oracleduel.settle.plist
```

`bin/daily.sh` is idempotent (a `state/.last_cycle` stamp prevents double runs,
so launchd's catch-up firing after wake is safe). Both scripts locate the repo
from their own path and read `.env` themselves.

**Linux (cron):**

```cron
0 7 * * * /path/to/bet/bin/daily.sh
0 19 * * * /path/to/bet/bin/settle.sh
```

## Kill switch

```bash
touch state/STOP    # blocks all order submission immediately; forecasting continues
```

## Risk engine (immutable — engine/risk.py)

Quarter-Kelly capped at 5% of bankroll · max 3 new positions/agent/day · 10% daily loss cap
· 15% drawdown circuit breaker (halts until human review) · min 3¢ edge net of Kalshi fees
· 2¢ slippage liquidity filter · self-match guard · STOP file. In production:
`chmod 444 engine/risk.py`. To un-halt an agent after human review:
`python -m engine.orchestrator resume`.

## Prompt evolution

A round = 7 days AND ≥100 resolved forecasts. At round close the agent writes a post-mortem
over its graded record, then proposes exactly ONE prompt change (linted: ≤500 words, no
risk vocabulary, single diff hunk). Versions are git-committed and tagged
`agent-{name}-v{N}`; round Brier is written into git notes. If version N scores worse than
N−1, it auto-reverts.

## Observability

- `python -m engine.orchestrator status` — one-line-per-agent summary
- `python -m engine.orchestrator dashboard` — writes `state/dashboard.html`
  (Brier by agent/version, calibration, P&L, profit per inference dollar, mutation history)
- alerts: set `NTFY_TOPIC` for push notifications; everything also lands in `logs/alerts.log`
- raw data: `state/telemetry.db` (forecasts, trades, prompt_versions, postmortems,
  token_spend, incidents)

## License

MIT — see [LICENSE](LICENSE). The telemetry data (forecasts, outcomes, reasoning traces)
is not in this repo; a full export is planned alongside the experiment write-up.
