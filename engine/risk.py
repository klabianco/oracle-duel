"""IMMUTABLE RISK ENGINE.

Agents have no read or write access to this file and their strategy prompts may not
reference anything it owns (sizing, limits, frequency). Enforced by:
  - prompt_guard.lint_prompt() rejecting risk vocabulary in prompt mutations
  - file permissions: `chmod 444 engine/risk.py` in production
  - code review: any diff touching this file requires human sign-off

Rules (constants below) are deliberately NOT in config.yaml.
"""

import math
from dataclasses import dataclass, field

RULES = {
    "kelly_fraction": 0.25,          # quarter-Kelly on the agent's stated edge
    "max_position_frac": 0.05,       # hard cap: 5% of agent bankroll per position
    "max_new_positions_per_day": 3,
    "daily_loss_cap_frac": 0.10,     # lose 10% in a day -> sit out until tomorrow
    "circuit_breaker_drawdown": 0.15,  # 15% off high-water -> halt until human review
    "min_net_edge": 0.03,            # net of fees and spread, else no trade
    "max_slippage": 0.02,            # order book must absorb position within 2 cents
    "fee_rate": 0.07,                # Kalshi taker fee coefficient: 0.07 * p * (1-p)
}


def fee_per_contract(price: float) -> float:
    """Kalshi trading fee per contract at a given price (worst near 50c)."""
    return RULES["fee_rate"] * price * (1.0 - price)


def total_fee(price: float, contracts: int) -> float:
    """Kalshi rounds total fees up to the next cent."""
    return math.ceil(fee_per_contract(price) * contracts * 100) / 100.0


def net_edge(prob_side: float, ask: float) -> float:
    """Expected value per contract for buying one side at `ask`, net of fees.

    prob_side = agent's probability that THIS side pays out.
    """
    return prob_side - ask - fee_per_contract(ask)


def kelly_contracts(prob_side: float, ask: float, bankroll: float) -> int:
    """Quarter-Kelly position size in contracts, capped at 5% of bankroll."""
    if ask <= 0 or ask >= 1:
        return 0
    f_star = (prob_side - ask) / (1.0 - ask)
    if f_star <= 0:
        return 0
    frac = min(f_star * RULES["kelly_fraction"], RULES["max_position_frac"])
    return int((frac * bankroll) // ask)


def book_depth_within_slippage(orderbook: dict, side: str, ask: float) -> int:
    """Contracts available to a `side` buyer within max_slippage of the best ask.

    Kalshi books list resting bids. A resting NO bid at price q fills a YES buyer
    at 1-q, so YES-side liquidity lives on the no_bids ladder and vice versa.
    """
    ladder = orderbook.get("no_bids" if side == "yes" else "yes_bids") or []
    limit = ask + RULES["max_slippage"]
    depth = 0
    for bid_price, qty in ladder:
        fill_price = round(1.0 - bid_price, 4)
        if fill_price <= limit:
            depth += qty
    return depth


@dataclass
class Order:
    agent: str
    market_id: str
    side: str          # 'yes' | 'no'
    contracts: int
    price: float       # limit price (the ask we evaluated)
    fees: float
    edge_net: float
    prob: float


@dataclass
class Decision:
    orders: list = field(default_factory=list)
    rejections: list = field(default_factory=list)   # (market_id, reason)
    halt: str | None = None                          # circuit-breaker reason, if tripped


def evaluate(agent: str, forecasts: list[dict], ctx: dict) -> Decision:
    """Convert an agent's forecasts into approved orders, or refuse.

    forecasts: [{market_id, prob, yes_ask, no_ask, ...}] — already scored by the agent.
    ctx: {
        stop_flag: bool,
        bankroll, high_water, day_start_bankroll: float,
        open_stake: float,   # cost basis (incl. fees) of open positions; equity = bankroll + open_stake
        trades_today: int,
        own_positions: {market_id: side},
        other_positions: {market_id: side},   # the OTHER agent's open/pending sides
        orderbook_fn: callable(market_id) -> orderbook dict,
    }
    """
    d = Decision()

    if ctx.get("stop_flag"):
        d.rejections.append(("*", "STOP file present — kill switch engaged"))
        return d

    bankroll = ctx["bankroll"]
    # Drawdown is measured on equity (cash + capital staked on open positions),
    # not cash alone: money riding on an open bet is at risk, not lost.
    equity = bankroll + ctx.get("open_stake", 0.0)
    if equity < ctx["high_water"] * (1 - RULES["circuit_breaker_drawdown"]):
        d.halt = (f"circuit breaker: equity {equity:.2f} (cash {bankroll:.2f} + "
                  f"open stakes {ctx.get('open_stake', 0.0):.2f}) is >15% below "
                  f"high-water {ctx['high_water']:.2f}; halted pending human review")
        d.rejections.append(("*", d.halt))
        return d

    day_start = ctx.get("day_start_bankroll") or bankroll
    if bankroll < day_start * (1 - RULES["daily_loss_cap_frac"]):
        d.rejections.append(("*", "daily loss cap hit — sitting out until next day"))
        return d

    budget = RULES["max_new_positions_per_day"] - ctx.get("trades_today", 0)
    if budget <= 0:
        d.rejections.append(("*", "max new positions per day already reached"))
        return d

    # Rank candidate trades by net edge; the agent's enthusiasm is irrelevant.
    candidates = []
    for f in forecasts:
        for side, ask, p_side in (
            ("yes", f.get("yes_ask"), f["prob"]),
            ("no", f.get("no_ask"), 1.0 - f["prob"]),
        ):
            if ask is None:
                continue
            e = net_edge(p_side, ask)
            if e >= RULES["min_net_edge"]:
                candidates.append((e, side, ask, p_side, f))
    candidates.sort(key=lambda c: c[0], reverse=True)

    seen_markets = set()
    for e, side, ask, p_side, f in candidates:
        if len(d.orders) >= budget:
            break
        mid = f["market_id"]
        if mid in seen_markets:
            continue
        seen_markets.add(mid)

        if mid in ctx.get("own_positions", {}):
            d.rejections.append((mid, "already holds a position in this market"))
            continue

        other = ctx.get("other_positions", {}).get(mid)
        if other and other != side:
            d.rejections.append((mid, "self-match guard: other agent is on the opposite side"))
            continue

        contracts = kelly_contracts(p_side, ask, bankroll)
        if contracts < 1:
            d.rejections.append((mid, "kelly size below one contract"))
            continue

        try:
            book = ctx["orderbook_fn"](mid)
        except Exception as ex:
            d.rejections.append((mid, f"orderbook unavailable: {ex}"))
            continue
        depth = book_depth_within_slippage(book, side, ask)
        if depth < contracts:
            d.rejections.append(
                (mid, f"insufficient liquidity: need {contracts}, book absorbs {depth} within 2c")
            )
            continue

        d.orders.append(Order(
            agent=agent, market_id=mid, side=side, contracts=contracts,
            price=ask, fees=total_fee(ask, contracts), edge_net=round(e, 4),
            prob=f["prob"],
        ))

    return d
