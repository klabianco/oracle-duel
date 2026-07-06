"""Daily orchestrator. Run from cron:  python -m engine.orchestrator cycle

Commands:
  cycle          full daily cycle (scan -> forecast -> score -> trade -> digest)
  postmortem     close rounds that are complete (runs automatically inside cycle too)
  status         print bankrolls, spend, forecast counts
  dashboard      regenerate state/dashboard.html
  fast-forward N advance the mock clock N days (mock mode only)
"""

import argparse
import sys
from datetime import datetime, timezone

from engine import gates, postmortem, risk, scanner, scorer
from engine.agent_runner import AgentRunner
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


def place_orders(agent: str, decision: risk.Decision, telemetry, client, cfg, date, alerts):
    """Submit (live) or simulate (paper) the approved orders. STOP is re-checked here."""
    for order in decision.orders:
        if stop_flag_set():
            telemetry.incident("stop_flag_block", agent, "STOP appeared before submission")
            alerts.send(f"[{agent}] order blocked: STOP file present")
            return
        order_id, paper = None, 1
        if cfg.get("live") and not cfg.get("mock"):
            try:
                resp = client.create_order(order.market_id, order.side,
                                           order.contracts, order.price)
                order_id = resp.get("order", {}).get("order_id")
                paper = 0
            except Exception as e:
                telemetry.incident("order_rejected", agent,
                                   {"market": order.market_id, "error": str(e)})
                alerts.send(f"[{agent}] ORDER REJECTED {order.market_id}: {e}")
                continue
        telemetry.record_trade(
            cycle_date=date, agent=agent, market_id=order.market_id, side=order.side,
            contracts=order.contracts, price=order.price, fees=order.fees,
            status="open", paper=paper, order_id=order_id,
        )
        # deduct cost + fees now; scorer credits the payout at settlement
        telemetry.adjust_bankroll(agent, -(order.contracts * order.price + order.fees))
        alerts.send(f"[{agent}] {'LIVE' if not paper else 'paper'} order: "
                    f"{order.contracts}x {order.side.upper()} {order.market_id} "
                    f"@ {order.price:.2f} (net edge {order.edge_net:+.3f})")


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


def cycle(cfg):
    telemetry = Telemetry(DB_PATH)
    alerts = Alerts(cfg)
    client = make_client(cfg)
    date = _now(client).date().isoformat()

    if not client.exchange_ok():
        telemetry.incident("exchange_down", None, "skipping cycle, never queueing stale orders")
        alerts.send("Kalshi API down/closed — cycle skipped")
        return

    # 1) settle yesterday first so bankrolls and error logs are current
    stats = scorer.score_resolutions(client, telemetry)

    # 2) shared market list
    markets = scanner.scan(client, cfg, now=_now(client))
    if not markets:
        alerts.send("scanner found no eligible markets — cycle ends")
        return

    agent_names = list(cfg["agents"].keys())
    for name in agent_names:
        telemetry.ensure_agent(name, cfg["bankroll_start"])
        telemetry.start_day(name, date)

    # 3) forecast everything, bet little
    runners, all_forecasts = {}, {}
    for name, agent_cfg in cfg["agents"].items():
        runner = AgentRunner(name, agent_cfg, cfg, telemetry)
        runners[name] = runner
        try:
            all_forecasts[name] = runner.run_cycle(markets, date, alerts)
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
            "trades_today": telemetry.trades_opened_on(name, date),
            "own_positions": telemetry.positions_by_market(name),
            "other_positions": other_positions,
            "orderbook_fn": client.get_orderbook,
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
    alerts.send("\n".join(lines), title="oracle-duel daily digest")
    generate_dashboard(telemetry, cfg)
    print("\n".join(lines))


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


def main():
    ap = argparse.ArgumentParser(prog="orchestrator")
    ap.add_argument("command", choices=["cycle", "postmortem", "status", "dashboard",
                                        "gate", "fast-forward"])
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
        print(gates.report(Telemetry(DB_PATH), cfg))
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
