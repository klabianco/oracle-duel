"""Scorer: grade resolved forecasts (Brier) and settle trades (P&L net of fees)."""


def _market_outcome(market: dict) -> int | None:
    if market.get("status") not in ("settled", "finalized"):
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return 1
    if result == "no":
        return 0
    return None


def score_resolutions(client, telemetry) -> dict:
    """Resolve forecasts and settle trades against current market state."""
    stats = {"forecasts_resolved": 0, "trades_settled": 0, "pnl": {}}
    market_cache = {}

    def market(mid):
        if mid not in market_cache:
            market_cache[mid] = client.get_market(mid)
        return market_cache[mid]

    for f in telemetry.unresolved_forecasts():
        try:
            m = market(f["market_id"])
        except Exception as e:
            telemetry.incident("resolution_fetch_error", f["agent"],
                              {"market": f["market_id"], "error": str(e)})
            continue
        outcome = _market_outcome(m)
        if outcome is None:
            continue
        brier = (f["prob"] - outcome) ** 2
        telemetry.resolve_forecast(f["id"], outcome, brier)
        stats["forecasts_resolved"] += 1

    for t in telemetry.open_trades():
        try:
            m = market(t["market_id"])
        except Exception:
            continue
        outcome = _market_outcome(m)
        if outcome is None:
            continue
        won = (outcome == 1 and t["side"] == "yes") or (outcome == 0 and t["side"] == "no")
        gross = t["contracts"] * (1 - t["price"]) if won else -t["contracts"] * t["price"]
        pnl = round(gross - t["fees"], 2)
        telemetry.settle_trade(t["id"], pnl)
        # cost + fees were deducted at entry; credit back the full payout on a win
        payout = t["contracts"] * 1.0 if won else 0.0
        telemetry.adjust_bankroll(t["agent"], payout)
        stats["trades_settled"] += 1
        stats["pnl"][t["agent"]] = round(stats["pnl"].get(t["agent"], 0) + pnl, 2)

    return stats
