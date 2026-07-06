"""Go/no-go gate metrics — pre-registered BEFORE the data arrives (2026-07-05).

The verdict question: can the agents beat the market price by enough to pay fees?

Metric 1 — paired Brier vs market: on every resolved forecast, the market's own
Brier (using the mid price at forecast time) minus the agent's Brier. Positive
delta = agent better calibrated than the market. Paired on identical markets and
outcomes, so it converges fast.

Metric 2 — realized edge on claims: on forecasts where the agent claimed a net
edge >= 3c, did outcomes land on the agent's side of the price? Mean of
sign * (outcome - market_price), gross of spread. This is the direct "would the
bets have made money" measure.

PRE-REGISTERED DECISION RULE (do not tune after seeing data):
  KILL     — n >= 300 resolved and the 95% CI upper bound of paired delta < +0.005
             and realized claim edge shows no positive signal.
             (A calibration edge under 0.005 Brier cannot pay Kalshi fees.)
  GO       — paired delta 95% CI excludes zero in the agent's favor, OR
             realized claim edge > 0.05 with one-sided 95% confidence, n_claims >= 100.
  AMBIGUOUS— anything else; keep papering. HARD STOP at 600 resolved: if still
             ambiguous, any edge is too small to trade — default to KILL.
"""

Z95 = 1.96
Z90_ONE_SIDED = 1.645
MIN_N = 300
HARD_STOP_N = 600
KILL_DELTA = 0.005
GO_CLAIM_EDGE = 0.05
MIN_CLAIMS = 100
CLAIM_EDGE_MIN = 0.03


def _mean_ci(xs: list[float], z: float = Z95):
    n = len(xs)
    if n < 2:
        return {"n": n, "mean": None, "lo": None, "hi": None, "se": None}
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    se = (var / n) ** 0.5
    return {"n": n, "mean": mean, "lo": mean - z * se, "hi": mean + z * se, "se": se}


def paired_brier(telemetry, agent: str) -> dict:
    rows = telemetry.conn.execute(
        "SELECT prob, market_price, outcome FROM forecasts "
        "WHERE agent=? AND resolved=1 AND market_price IS NOT NULL", (agent,)).fetchall()
    ds = [(r["market_price"] - r["outcome"]) ** 2 - (r["prob"] - r["outcome"]) ** 2
          for r in rows]
    return _mean_ci(ds)


def realized_claim_edge(telemetry, agent: str) -> dict:
    rows = telemetry.conn.execute(
        "SELECT prob, market_price, outcome FROM forecasts "
        "WHERE agent=? AND resolved=1 AND market_price IS NOT NULL AND edge_net >= ?",
        (agent, CLAIM_EDGE_MIN)).fetchall()
    es = []
    for r in rows:
        side = 1 if r["prob"] > r["market_price"] else -1
        es.append(side * (r["outcome"] - r["market_price"]))
    return _mean_ci(es)


def verdict(pb: dict, ce: dict) -> str:
    n = pb["n"]
    if n < MIN_N:
        return f"INSUFFICIENT DATA (n={n}, rule engages at {MIN_N})"
    claims_positive = (ce["n"] >= MIN_CLAIMS and ce["mean"] is not None
                       and ce["mean"] - Z90_ONE_SIDED * ce["se"] > GO_CLAIM_EDGE)
    calib_positive = pb["lo"] is not None and pb["lo"] > 0
    if calib_positive or claims_positive:
        return "GO signal — edge confirmed under pre-registered rule"
    claims_dead = ce["n"] < 10 or (ce["mean"] is not None and ce["mean"] <= 0)
    if pb["hi"] is not None and pb["hi"] < KILL_DELTA and claims_dead:
        return "KILL signal — tradeable edge ruled out under pre-registered rule"
    if n >= HARD_STOP_N:
        return "HARD STOP — still ambiguous at 600 resolved: default KILL"
    return f"AMBIGUOUS — keep papering (n={n}, hard stop at {HARD_STOP_N})"


def report(telemetry, cfg: dict) -> str:
    lines = ["=== GO/NO-GO GATE (pre-registered 2026-07-05) ==="]
    for agent in cfg["agents"]:
        pb = paired_brier(telemetry, agent)
        ce = realized_claim_edge(telemetry, agent)
        if pb["mean"] is None:
            lines.append(f"[{agent}] n={pb['n']} resolved — too few to compute")
            continue
        lines.append(
            f"[{agent}] paired Δbrier vs market: {pb['mean']:+.4f} "
            f"[{pb['lo']:+.4f}, {pb['hi']:+.4f}] (n={pb['n']})")
        if ce["mean"] is not None:
            lines.append(
                f"[{agent}] realized edge on ≥3c claims: {ce['mean']:+.3f} "
                f"± {ce['se']:.3f} (n={ce['n']})")
        else:
            lines.append(f"[{agent}] realized edge on ≥3c claims: n={ce['n']} — too few")
        lines.append(f"[{agent}] {verdict(pb, ce)}")
    return "\n".join(lines)
