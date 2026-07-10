"""Daily orchestrator. Run from cron:  python -m engine.orchestrator cycle

Commands:
  cycle          full daily cycle (scan -> forecast -> score -> trade -> digest)
  postmortem     close rounds that are complete (runs automatically inside cycle too)
  status         print bankrolls, spend, forecast counts
  settle         settle-only pass: grade forecasts/trades that finalized since the cycle
  dashboard      regenerate state/dashboard.html
  fast-forward N advance the mock clock N days (mock mode only)
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from engine import audit_metrics, gates, postmortem, risk, scanner, scorer
from engine.agent_runner import AgentRunner, market_ineligible
from engine.alerts import Alerts
from engine.config import DB_PATH, STATE_DIR, load_config, stop_flag_set
from engine.dashboard import generate as generate_dashboard
from engine.kalshi_client import KalshiClient, MockKalshiClient
from engine.telemetry import Telemetry


def make_client(cfg):
    if cfg.get("mock"):
        return MockKalshiClient(STATE_DIR / "mock_state.json")
    return KalshiClient()


def _now(client) -> datetime:
    return getattr(client, "now", lambda: datetime.now(timezone.utc))()


def _cycle_date(client, cfg) -> str:
    override = os.environ.get("ORACLE_CYCLE_DATE")
    if override:
        return override
    tz = ZoneInfo(cfg.get("timezone", "America/Los_Angeles"))
    return _now(client).astimezone(tz).date().isoformat()


def _paper_fill(client, order: risk.Order) -> tuple[int, float] | None:
    """IOC-style fill against the executable book within the immutable
    engine's slippage bound. Mirrors live semantics: partial fills are
    taken (truncated to whole contracts, like the live path's int()), and
    None means nothing filled at all."""
    book = client.get_orderbook(order.market_id)
    ladder = book.get("no_bids" if order.side == "yes" else "yes_bids") or []
    limit = order.price + risk.RULES["max_slippage"]
    levels = sorted((round(1.0 - p, 4), q) for p, q in ladder)
    available = sum(q for price, q in levels if price <= limit)
    contracts = min(order.contracts, int(available))
    if contracts <= 0:
        return None
    remaining, cost = contracts, 0.0
    for fill_price, qty in levels:
        if fill_price > limit or remaining <= 0:
            continue
        filled = min(remaining, qty)
        remaining -= filled
        cost += filled * fill_price
    return contracts, round(cost / contracts, 4)


def place_orders(agent: str, decision: risk.Decision, telemetry, client, cfg, date, alerts):
    """Submit (live) or simulate (paper) the approved orders. STOP is re-checked here."""
    for order in decision.orders:
        if stop_flag_set():
            telemetry.incident("stop_flag_block", agent, "STOP appeared before submission")
            alerts.send(f"[{agent}] order blocked: STOP file present")
            return
        order_id, paper = None, 1
        contracts, fill_price, fees = order.contracts, order.price, order.fees
        if cfg.get("live") and not cfg.get("mock"):
            try:
                # IOC at exactly the risk-approved price: the immutable engine
                # sized the position at order.price, so fills above it would
                # breach the approved position cost. max_slippage governs the
                # engine's own depth checks, not an authority to pay more.
                resp = client.create_order(order.market_id, order.side,
                                           order.contracts, order.price)
                order_id = resp.get("order_id")
                contracts = int(resp.get("filled_count") or 0)
                if contracts <= 0:
                    telemetry.incident(
                        "order_unfilled", agent,
                        {"market": order.market_id, "order_id": order_id},
                    )
                    continue
                fill_price = resp.get("average_fill_price") or order.price
                fees = resp.get("fees_paid")
                if fees is None:
                    fees = risk.total_fee(fill_price, contracts)
                if contracts < order.contracts:
                    telemetry.incident(
                        "order_partial_fill", agent,
                        {"market": order.market_id, "requested": order.contracts,
                         "filled": contracts, "order_id": order_id},
                    )
                paper = 0
            except Exception as e:
                telemetry.incident("order_rejected", agent,
                                   {"market": order.market_id, "error": str(e)})
                alerts.send(f"[{agent}] ORDER REJECTED {order.market_id}: {e}")
                continue
        elif not cfg.get("mock"):
            try:
                fill = _paper_fill(client, order)
            except Exception as e:
                telemetry.incident(
                    "paper_fill_error", agent,
                    {"market": order.market_id, "error": str(e)},
                )
                continue
            if fill is None:
                telemetry.incident(
                    "paper_order_unfilled", agent,
                    {"market": order.market_id, "requested": order.contracts},
                )
                continue
            contracts, fill_price = fill
            if contracts < order.contracts:
                telemetry.incident(
                    "paper_partial_fill", agent,
                    {"market": order.market_id, "requested": order.contracts,
                     "filled": contracts},
                )
            fees = risk.total_fee(fill_price, contracts)
        telemetry.record_trade(
            cycle_date=date, agent=agent, market_id=order.market_id, side=order.side,
            contracts=contracts, price=fill_price, fees=fees,
            status="open", paper=paper, order_id=order_id,
        )
        # deduct cost + fees now; scorer credits the payout at settlement
        telemetry.adjust_bankroll(agent, -(contracts * fill_price + fees))
        alerts.send(f"[{agent}] {'LIVE' if not paper else 'paper'} order: "
                    f"{contracts}x {order.side.upper()} {order.market_id} "
                    f"@ {fill_price:.4f} (forecast-time net edge {order.edge_net:+.3f})")


def scoreboard(telemetry, cfg) -> str | None:
    """Head-to-head score after a round closes: latest round Brier + cumulative money.

    Returns None until every agent has at least one closed round.
    """
    rows = []
    for name in cfg["agents"]:
        last = telemetry.conn.execute(
            "SELECT version, round_brier, round_n, reverted FROM prompt_versions "
            "WHERE agent=? AND round_brier IS NOT NULL ORDER BY version DESC LIMIT 1",
            (name,)).fetchone()
        if not last:
            return None
        spend = telemetry.total_spend(name)
        pnl = telemetry.total_pnl(name)
        rows.append({
            "agent": name, "version": last["version"], "brier": last["round_brier"],
            "n": last["round_n"], "reverted": bool(last["reverted"]),
            "pnl": pnl, "spend": spend,
            "ppd": (pnl / spend) if spend else None,
        })

    by_brier = sorted(rows, key=lambda r: r["brier"])
    lead, trail = by_brier[0], by_brier[-1]
    lines = ["=== ROUND SCOREBOARD ==="]
    for r in rows:
        ppd = f"{r['ppd']:+.2f}" if r["ppd"] is not None else "n/a"
        lines.append(
            f"[{r['agent']}] round brier {r['brier']:.4f} over {r['n']} forecasts "
            f"(v{r['version']}{', reverted' if r['reverted'] else ''}) | "
            f"total P&L ${r['pnl']:+.2f} | spend ${r['spend']:.2f} | P&L per $ {ppd}")
    if abs(lead["brier"] - trail["brier"]) < 1e-9:
        lines.append("calibration: dead even")
    else:
        lines.append(f"calibration lead: {lead['agent']} by "
                     f"{trail['brier'] - lead['brier']:.4f} brier")
    by_pnl = sorted(rows, key=lambda r: r["pnl"], reverse=True)
    if abs(by_pnl[0]["pnl"] - by_pnl[-1]["pnl"]) > 0.005:
        lines.append(f"money lead: {by_pnl[0]['agent']} by "
                     f"${by_pnl[0]['pnl'] - by_pnl[-1]['pnl']:+.2f}")
    return "\n".join(lines)


def plain_digest(telemetry, cfg, date, n_markets, all_forecasts) -> str:
    """The daily message, written for a human glancing at a phone — plain words only."""
    live = cfg.get("live") and not cfg.get("mock")
    bet_word = "real bet" if live else "pretend bet"
    out = [f"Oracle Duel — {date} ({'REAL money' if live else 'pretend money'})", "",
           f"The robots studied {n_markets} markets today.", ""]

    for name in cfg["agents"]:
        st = telemetry.agent_state(name)
        trades = [dict(r) for r in telemetry.conn.execute(
            "SELECT * FROM trades WHERE agent=? AND cycle_date=? AND status!='rejected'",
            (name, date))]
        out.append(name.upper())
        out.append(f"- Made {len(all_forecasts.get(name, []))} guesses today.")
        if trades:
            out.append(f"- Placed {len(trades)} {bet_word}{'s' if len(trades) > 1 else ''}:")
            for t in trades:
                cost = t["contracts"] * t["price"] + t["fees"]
                row = telemetry.conn.execute(
                    "SELECT market_title FROM forecasts WHERE market_id=? AND agent=? "
                    "ORDER BY id DESC", (t["market_id"], name)).fetchone()
                title = row["market_title"] if row else t["market_id"]
                out.append(f'  * ${cost:.2f} says {t["side"].upper()} on "{title[:70]}"')
        elif st["halted"]:
            out.append("- Placed no bets — betting is paused.")
        else:
            out.append("- Placed no bets. No idea was strong enough to beat the fees.")
        if st["halted"]:
            out.append("- PAUSED: this robot lost too much and needs your OK to bet again.")
        riding = telemetry.conn.execute(
            "SELECT COALESCE(SUM(contracts*price+fees),0), COUNT(*) FROM trades "
            "WHERE agent=? AND status='open'", (name,)).fetchone()
        start = cfg["bankroll_start"]
        if riding[1]:
            out.append(f"- Money: ${st['bankroll']:.2f} in the wallet, plus "
                       f"${riding[0]:.2f} riding on {riding[1]} open "
                       f"bet{'s' if riding[1] > 1 else ''} (started with ${start:.0f})")
        else:
            total = st["bankroll"]
            word = "up" if total > start + 0.005 else ("down" if total < start - 0.005 else "even")
            out.append(f"- Money: ${total:.2f} — {word} vs the ${start:.0f} start")
        out.append(f"- Thinking cost today: ${telemetry.spend_on(name, date):.2f}")
        out.append("")

    resolved_today = telemetry.conn.execute(
        "SELECT agent, market_id, prob, outcome FROM forecasts "
        "WHERE resolved=1 AND substr(resolved_at,1,10)=?", (date,)).fetchall()
    if resolved_today:
        out.append("SCOREKEEPING")
        # count distinct markets: an agent may lack a row for a market the other
        # forecast (e.g. after a telemetry reset), so dividing by agent count is wrong
        n_res = len({r["market_id"] for r in resolved_today})
        out.append(f"- {n_res} old guess{'es' if n_res != 1 else ''} "
                   "got their answers today.")
        for name in cfg["agents"]:
            mine = [r for r in resolved_today if r["agent"] == name]
            right = sum(1 for r in mine
                        if (r["outcome"] == 1) == (r["prob"] > 0.5))
            if mine:
                out.append(f"- {name} called {right} of {len(mine)} right.")
        out.append("")

    out.append("THE BIG QUESTION: can they beat the market?")
    for name in cfg["agents"]:
        pb = gates.paired_brier(telemetry, name)
        n = pb["n"]
        if n < gates.MIN_N:
            todays = sum(1 for r in resolved_today if r["agent"] == name)
            if todays:
                days_left = (gates.MIN_N - n) / todays
                eta = f" We should know around {_eta_date(date, days_left)}."
            else:
                eta = ""
            out.append(f"- {name}: too early to tell ({n} of {gates.MIN_N} answers in).{eta}")
        else:
            v = gates.verdict(pb, gates.realized_claim_edge(telemetry, name))
            if v.startswith("GO"):
                out.append(f"- {name}: YES — it is beating the market. "
                           "Time to talk about real money.")
            elif v.startswith(("KILL", "HARD STOP")):
                out.append(f"- {name}: NO — it is not beating the market. "
                           "Probably time to stop.")
            else:
                out.append(f"- {name}: too close to call ({n} of "
                           f"{gates.HARD_STOP_N} answers in).")
    return "\n".join(out)


def _eta_date(date: str, days_left: float) -> str:
    from datetime import date as d, timedelta
    return (d.fromisoformat(date) + timedelta(days=round(days_left))).strftime("%b %d")


def cycle(cfg):
    telemetry = Telemetry(DB_PATH)
    alerts = Alerts(cfg)
    client = make_client(cfg)
    date = _cycle_date(client, cfg)

    if not client.exchange_ok():
        telemetry.incident("exchange_down", None, "skipping cycle, never queueing stale orders")
        alerts.send("Kalshi API down/closed — cycle skipped")
        return

    # 1) settle yesterday first so bankrolls and error logs are current
    stats = scorer.score_resolutions(client, telemetry)

    # 2) shared market list
    markets = scanner.scan(
        client, cfg, now=_now(client),
        exclude_market_ids=telemetry.forecasted_market_ids(),
    )
    # Re-check eligibility with fresh data once, centrally, so both agents
    # receive the identical final list (symmetry ground rule). The per-agent
    # check in run_cycle remains only as a last-resort guard for markets that
    # flip during the run itself.
    screened = []
    for m in markets:
        try:
            fresh = client.get_market(m["market_id"])
        except Exception:
            screened.append(m)  # transient fetch error — agents re-check anyway
            continue
        reason = market_ineligible(fresh, now=_now(client))
        if reason:
            telemetry.incident("prescreen_dropped", None,
                               {"market": m["market_id"], "reason": reason})
            continue
        screened.append(m)
    markets = screened
    if not markets:
        alerts.send("scanner found no eligible markets — cycle ends")
        return

    # Claim the date only now, right before paid inference. Settle, scan and
    # pre-screen are idempotent and safe to retry after a transient failure;
    # agent inference must never run twice for one date.
    if not telemetry.claim_cycle(date):
        telemetry.incident("duplicate_cycle_blocked", None, {"cycle_date": date})
        print(f"cycle {date}: already claimed; refusing to spend inference again")
        return
    try:
        _paid_cycle(cfg, telemetry, alerts, client, date, markets, stats)
    except Exception as e:
        telemetry.complete_cycle(date, "failed", str(e))
        raise
    telemetry.complete_cycle(date)


def _paid_cycle(cfg, telemetry, alerts, client, date, markets, stats):
    """Everything downstream of the date claim: agent inference, orders,
    round close, digest. The caller marks the claim 'failed' on any raise."""
    alerts.send(f"Good morning — the robots are starting work on {len(markets)} markets. "
                "Results in an hour or two.", title="oracle-duel: cycle started")

    agent_names = list(cfg["agents"].keys())
    # Alternate who researches/bets first each day: shared-resource failures
    # (search quotas) and order-dependent effects must not always land on the
    # same agent. Date parity keeps it deterministic and re-run-safe.
    if datetime.fromisoformat(date).toordinal() % 2:
        agent_names.reverse()
    for name in agent_names:
        telemetry.ensure_agent(name, cfg["bankroll_start"])
        telemetry.start_day(name, date)

    # 3) forecast everything, bet little
    runners, all_forecasts = {}, {}
    for name in agent_names:
        agent_cfg = cfg["agents"][name]
        runner = AgentRunner(name, agent_cfg, cfg, telemetry)
        runners[name] = runner
        try:
            all_forecasts[name] = runner.run_cycle(
                markets, date, alerts, refresh_market=client.get_market)
        except Exception as e:
            telemetry.incident("agent_cycle_error", name, str(e))
            alerts.send(f"[{name}] cycle failed: {e}")
            all_forecasts[name] = []

    # 4) risk engine converts top edges into orders (or refuses)
    for name in agent_names:
        st = telemetry.agent_state(name)
        if st["halted"]:
            alerts.send(f"[{name}] halted ({st['halted_reason']}) — no orders")
            continue
        other = [a for a in agent_names if a != name]
        other_positions = {}
        for o in other:
            other_positions.update(telemetry.positions_by_market(o))
        ctx = {
            "stop_flag": stop_flag_set(),
            "bankroll": st["bankroll"],
            "high_water": st["high_water"],
            "day_start_bankroll": st["day_start_bankroll"],
            "open_stake": telemetry.open_stake(name),
            "trades_today": telemetry.trades_opened_on(name, date),
            "own_positions": telemetry.positions_by_market(name),
            "other_positions": other_positions,
            "orderbook_fn": client.get_orderbook,
            "live": bool(cfg.get("live")),
        }
        decision = risk.evaluate(name, all_forecasts[name], ctx)
        if decision.halt:
            telemetry.set_halted(name, True, decision.halt)
            telemetry.incident("circuit_breaker", name, decision.halt)
            alerts.send(f"[{name}] CIRCUIT BREAKER: {decision.halt}")
            continue
        for mid, reason in decision.rejections:
            telemetry.incident("order_refused", name, {"market": mid, "reason": reason})
        place_orders(name, decision, telemetry, client, cfg, date, alerts)

    # 5) close any completed rounds (prompt evolution)
    rounds_closed = False
    for name, runner in runners.items():
        if postmortem.round_complete(telemetry, name, cfg, now=_now(client)):
            result = postmortem.run_postmortem(runner, telemetry, cfg, alerts,
                                               now=_now(client))
            telemetry.incident("round_closed", name, result)
            rounds_closed = True
    if rounds_closed:
        sb = scoreboard(telemetry, cfg)
        if sb:
            sb += "\n" + gates.report(telemetry, cfg)
            sb += "\n" + audit_metrics.report(telemetry, cfg)
            alerts.send(sb, title="oracle-duel round scoreboard")
            print(sb)

    # 6) digest
    lines = [f"cycle {date}: {len(markets)} markets scanned, "
             f"{stats['forecasts_resolved']} forecasts resolved, "
             f"{stats['trades_settled']} trades settled"]
    for name in agent_names:
        st = telemetry.agent_state(name)
        n_forecasts = len(all_forecasts.get(name, []))
        spend = telemetry.spend_on(name, date)
        health = telemetry.tool_health_on(name, date)
        hs = " ".join(f"{h['tool']} {h['ok']}/{h['ok']+h['err']}" for h in health) or "no tool calls"
        lines.append(f"[{name}] bankroll ${st['bankroll']:.2f}, {n_forecasts} forecasts, "
                     f"spend ${spend:.2f} | research: {hs}")
        for h in health:
            total = h["ok"] + h["err"]
            if h["tool"] == "web_search" and total >= 5 and h["err"] / total > 0.5:
                alerts.send(f"[{name}] RESEARCH DEGRADED: web_search failing "
                            f"{h['err']}/{total} — agents are forecasting blind")
        pb = gates.paired_brier(telemetry, name)
        if pb["mean"] is not None:
            lines.append(f"[{name}] gate: Δbrier vs market {pb['mean']:+.4f} "
                         f"[{pb['lo']:+.4f}, {pb['hi']:+.4f}] n={pb['n']}")
    # technical digest -> log/stdout; plain-language digest -> the push notification
    print("\n".join(lines))
    print(audit_metrics.report(telemetry, cfg))
    digest = plain_digest(telemetry, cfg, date, len(markets), all_forecasts)
    alerts.send(digest, title="oracle-duel daily digest")
    generate_dashboard(telemetry, cfg)


def status(cfg):
    telemetry = Telemetry(DB_PATH)
    for name in cfg["agents"]:
        st = telemetry.agent_state(name)
        if not st:
            print(f"{name}: not initialized")
            continue
        brier, n = telemetry.conn.execute(
            "SELECT AVG(brier), COUNT(*) FROM forecasts WHERE agent=? AND resolved=1",
            (name,)).fetchone()
        cur = telemetry.current_version(name)
        print(f"{name}: bankroll ${st['bankroll']:.2f} hw ${st['high_water']:.2f} "
              f"halted={bool(st['halted'])} prompt v{cur['version'] if cur else '?'} "
              f"brier={f'{brier:.4f}' if brier is not None else 'n/a'} (n={n}) "
              f"pnl ${telemetry.total_pnl(name):.2f} spend ${telemetry.total_spend(name):.2f}")


def resume(cfg):
    """Clear circuit-breaker halts. This IS the 'human review' the halt asks for."""
    telemetry = Telemetry(DB_PATH)
    for name in cfg["agents"]:
        st = telemetry.agent_state(name)
        if not st or not st["halted"]:
            print(f"{name}: not halted")
            continue
        telemetry.set_halted(name, False)
        telemetry.incident("halt_cleared", name, st["halted_reason"])
        print(f"{name}: halt cleared (was: {st['halted_reason']})")


def main():
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("command", choices=["cycle", "postmortem", "status", "dashboard",
                                        "gate", "resume", "settle", "fast-forward"])
    ap.add_argument("days", nargs="?", type=int, default=1)
    ap.add_argument("--config", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.command == "cycle":
        cycle(cfg)
    elif args.command == "status":
        status(cfg)
    elif args.command == "dashboard":
        print(generate_dashboard(Telemetry(DB_PATH), cfg))
    elif args.command == "gate":
        telemetry = Telemetry(DB_PATH)
        print(gates.report(telemetry, cfg))
        print(audit_metrics.report(telemetry, cfg))
    elif args.command == "resume":
        resume(cfg)
    elif args.command == "settle":
        # settle-only pass (evening launchd job): grades whatever finalized
        # since the morning cycle so trades don't wait a full day
        telemetry = Telemetry(DB_PATH)
        stats = scorer.score_resolutions(make_client(cfg), telemetry)
        report = gates.report(telemetry, cfg)
        integrity = audit_metrics.report(telemetry, cfg)
        generate_dashboard(telemetry, cfg)
        print(f"settle: {stats['forecasts_resolved']} forecasts resolved, "
              f"{stats['trades_settled']} trades settled, pnl {stats['pnl']}")
        print(report)
        print(integrity)
        if stats["forecasts_resolved"] or stats["trades_settled"]:
            Alerts(cfg).send(
                f"Evening settlement: {stats['forecasts_resolved']} forecasts resolved, "
                f"{stats['trades_settled']} trades settled.\n\n{report}\n\n{integrity}",
                title="oracle-duel evening settlement",
            )
    elif args.command == "postmortem":
        telemetry = Telemetry(DB_PATH)
        alerts = Alerts(cfg)
        client = make_client(cfg)
        closed = False
        for name, agent_cfg in cfg["agents"].items():
            runner = AgentRunner(name, agent_cfg, cfg, telemetry)
            runner.load_prompt()
            if postmortem.round_complete(telemetry, name, cfg, now=_now(client)):
                print(postmortem.run_postmortem(runner, telemetry, cfg, alerts,
                                                now=_now(client)))
                closed = True
            else:
                print(f"{name}: round not complete yet")
        if closed:
            sb = scoreboard(telemetry, cfg)
            if sb:
                sb += "\n" + gates.report(telemetry, cfg)
                sb += "\n" + audit_metrics.report(telemetry, cfg)
                alerts.send(sb, title="oracle-duel round scoreboard")
                print(sb)
    elif args.command == "fast-forward":
        if not cfg.get("mock"):
            sys.exit("fast-forward only works in mock mode")
        client = make_client(cfg)
        client.fast_forward(args.days)
        print(f"mock clock advanced {args.days}d -> {client.now().isoformat()}")


if __name__ == "__main__":
    main()
