from engine import risk


def big_book(mid=None):
    # consistent with yes_ask=0.55 / no_ask=0.47: a resting NO bid at 0.45 fills a
    # YES buyer at 0.55; a resting YES bid at 0.53 fills a NO buyer at 0.47
    return {"yes_bids": [(0.53, 1000)], "no_bids": [(0.45, 1000)]}


def base_ctx(**over):
    ctx = {
        "stop_flag": False,
        "bankroll": 100.0,
        "high_water": 100.0,
        "day_start_bankroll": 100.0,
        "trades_today": 0,
        "own_positions": {},
        "other_positions": {},
        "orderbook_fn": big_book,
    }
    ctx.update(over)
    return ctx


def forecast(prob=0.70, yes_ask=0.55, no_ask=0.47, mid="M1"):
    return {"market_id": mid, "prob": prob, "yes_ask": yes_ask, "no_ask": no_ask}


def test_fee_worst_at_50c():
    assert risk.fee_per_contract(0.5) > risk.fee_per_contract(0.1)
    assert abs(risk.fee_per_contract(0.5) - 0.0175) < 1e-9


def test_kelly_capped_at_5pct():
    # huge edge: quarter-Kelly would exceed 5%, must be capped
    n = risk.kelly_contracts(0.95, 0.30, 100.0)
    assert n * 0.30 <= 5.0 + 1e-9
    assert n >= 1


def test_no_trade_below_min_edge():
    d = risk.evaluate("a", [forecast(prob=0.58, yes_ask=0.55, no_ask=0.47)], base_ctx())
    assert d.orders == []


def test_trade_placed_on_real_edge():
    d = risk.evaluate("a", [forecast(prob=0.75, yes_ask=0.55)], base_ctx())
    assert len(d.orders) == 1
    o = d.orders[0]
    assert o.side == "yes" and o.contracts >= 1
    assert o.fees > 0


def test_stop_flag_blocks_everything():
    d = risk.evaluate("a", [forecast(prob=0.95, yes_ask=0.30)], base_ctx(stop_flag=True))
    assert d.orders == [] and d.halt is None


def test_daily_loss_cap():
    d = risk.evaluate("a", [forecast(prob=0.95, yes_ask=0.30)],
                      base_ctx(bankroll=89.0, day_start_bankroll=100.0, high_water=100.0))
    assert d.orders == []


def test_circuit_breaker_halts():
    d = risk.evaluate("a", [forecast(prob=0.95, yes_ask=0.30)],
                      base_ctx(bankroll=80.0, high_water=100.0, day_start_bankroll=82.0))
    assert d.halt is not None and d.orders == []


def test_max_three_positions_per_day():
    fs = [forecast(prob=0.85, yes_ask=0.55, mid=f"M{i}") for i in range(6)]
    d = risk.evaluate("a", fs, base_ctx())
    assert len(d.orders) == 3


def test_self_match_guard():
    ctx = base_ctx(other_positions={"M1": "no"})
    d = risk.evaluate("a", [forecast(prob=0.85, yes_ask=0.55)], ctx)
    assert d.orders == []
    assert any("self-match" in r for _, r in d.rejections)


def test_liquidity_filter():
    thin = lambda mid: {"yes_bids": [(0.40, 1)], "no_bids": [(0.40, 1)]}
    d = risk.evaluate("a", [forecast(prob=0.85, yes_ask=0.55)],
                      base_ctx(orderbook_fn=thin))
    assert d.orders == []
    assert any("liquidity" in r for _, r in d.rejections)
