"""Scorer: grade resolved forecasts (Brier) and settle trades (P&L net of fees)."""


def _market_settlement(market: dict):
    """('binary', 0|1) | ('scalar', value 0..1) | None if not yet settled.

    Kalshi can settle a market without a yes/no result — e.g. a cancelled
    sports game resolved "to a fair price" per its rules_secondary. Those
    settle trades at settlement_value but cannot be Brier-scored.
    """
    if market.get("status") not in ("settled", "finalized"):
        return None
    result = (market.get("result") or "").lower()
    if result == "yes":
        return ("binary", 1)
    if result == "no":
        return ("binary", 0)
    # Require the explicit scalar result: a settled market can transiently
    # report result='' (possibly with a default settlement_value of 0), and
    # voiding/settling on that would be irreversible.
    if result == "scalar":
        value = market.get("settlement_value")
        if value is not None:
            return ("scalar", float(value))
    return None


def score_resolutions(client, telemetry) -> dict:
    """Resolve forecasts and settle trades against current market state."""
    stats = {"forecasts_resolved": 0, "forecasts_voided": 0,
             "trades_settled": 0, "pnl": {}}
    market_cache = {}
    scalar_logged = set()

    def market(mid):
        if mid not in market_cache:
            market_cache[mid] = client.get_market(mid)
        return market_cache[mid]

    def note_scalar(mid, agent, value):
        if mid not in scalar_logged:
            scalar_logged.add(mid)
            telemetry.incident("scalar_settlement", agent,
                              {"market": mid, "settlement_value": value})

    for f in telemetry.unresolved_forecasts():
        try:
            m = market(f["market_id"])
        except Exception as e:
            telemetry.incident("resolution_fetch_error", f["agent"],
                              {"market": f["market_id"], "error": str(e)})
            continue
        settlement = _market_settlement(m)
        if settlement is None:
            continue
        kind, value = settlement
        if kind == "scalar":
            telemetry.void_forecast(f["id"], f"scalar settlement at {value}")
            note_scalar(f["market_id"], f["agent"], value)
            stats["forecasts_voided"] += 1
            continue
        brier = (f["prob"] - value) ** 2
        telemetry.resolve_forecast(f["id"], value, brier)
        stats["forecasts_resolved"] += 1

    for t in telemetry.open_trades():
        try:
            m = market(t["market_id"])
        except Exception:
            continue
        settlement = _market_settlement(m)
        if settlement is None:
            continue
        kind, value = settlement
        if kind == "binary":
            won = (value == 1 and t["side"] == "yes") or (value == 0 and t["side"] == "no")
            side_value = 1.0 if won else 0.0
        else:  # scalar: each contract pays the settlement value for its side
            side_value = value if t["side"] == "yes" else 1.0 - value
            note_scalar(t["market_id"], t["agent"], value)
        pnl = round(t["contracts"] * (side_value - t["price"]) - t["fees"], 2)
        telemetry.settle_trade(t["id"], pnl)
        # cost + fees were deducted at entry; credit back the full payout
        telemetry.adjust_bankroll(t["agent"], t["contracts"] * side_value)
        stats["trades_settled"] += 1
        stats["pnl"][t["agent"]] = round(stats["pnl"].get(t["agent"], 0) + pnl, 2)

    return stats
