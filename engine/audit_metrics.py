"""Integrity diagnostics beside, but never inside, the pre-registered gate."""

import re


def _series_key(row) -> str:
    if row["series_ticker"]:
        return row["series_ticker"]
    if row["event_ticker"]:
        return row["event_ticker"].split("-")[0]
    return re.split(r"-(?:26|27)[A-Z0-9]", row["market_id"], maxsplit=1)[0]


def agent_integrity(telemetry, agent: str) -> dict:
    rows = telemetry.conn.execute(
        "SELECT cycle_date, market_id, event_ticker, series_ticker FROM forecasts "
        "WHERE agent=? AND resolved=1", (agent,),
    ).fetchall()
    markets = {r["market_id"] for r in rows}
    clusters = {(r["cycle_date"], _series_key(r)) for r in rows}
    return {
        "rows": len(rows),
        "unique_markets": len(markets),
        "same_day_series_clusters": len(clusters),
        "repeated_market_rows": len(rows) - len(markets),
        "cluster_excess_rows": len(rows) - len(clusters),
    }


def cluster_adjusted_paired_brier(telemetry, agent: str) -> dict:
    """Sensitivity CI with arbitrary dependence inside same-day series clusters."""
    rows = telemetry.conn.execute(
        "SELECT cycle_date, market_id, event_ticker, series_ticker, prob, "
        "market_price, outcome FROM forecasts WHERE agent=? AND resolved=1 "
        "AND market_price IS NOT NULL", (agent,),
    ).fetchall()
    if not rows:
        return {"n": 0, "clusters": 0, "mean": None, "lo": None, "hi": None}
    repeated = {}
    for r in rows:
        repeated[r["market_id"]] = repeated.get(r["market_id"], 0) + 1
    values = []
    grouped = {}
    for r in rows:
        value = ((r["market_price"] - r["outcome"]) ** 2
                 - (r["prob"] - r["outcome"]) ** 2)
        values.append(value)
        key = (("market", r["market_id"]) if repeated[r["market_id"]] > 1
               else ("series", r["cycle_date"], _series_key(r)))
        grouped.setdefault(key, []).append(value)
    n, groups = len(values), len(grouped)
    mean = sum(values) / n
    if groups < 2:
        return {"n": n, "clusters": groups, "mean": mean, "lo": None, "hi": None}
    scores = [sum(v - mean for v in xs) for xs in grouped.values()]
    variance = (groups / (groups - 1)) * sum(s * s for s in scores) / (n * n)
    se = variance ** 0.5
    return {
        "n": n, "clusters": groups, "mean": mean,
        "lo": mean - 1.96 * se, "hi": mean + 1.96 * se,
    }


def matched_head_to_head(telemetry, agents: list[str]) -> dict | None:
    if len(agents) != 2:
        return None
    a, b = agents
    rows = telemetry.conn.execute(
        "SELECT fa.market_id, fa.cycle_date, fa.brier a_brier, fb.brier b_brier, "
        "fa.market_price a_price, fb.market_price b_price "
        "FROM forecasts fa JOIN forecasts fb "
        "ON fa.market_id=fb.market_id AND fa.cycle_date=fb.cycle_date "
        "WHERE fa.agent=? AND fb.agent=? AND fa.resolved=1 AND fb.resolved=1 "
        "AND fa.outcome=fb.outcome", (a, b),
    ).fetchall()
    if not rows:
        return None
    total_a = telemetry.conn.execute(
        "SELECT COUNT(*) FROM forecasts WHERE agent=? AND resolved=1", (a,),
    ).fetchone()[0]
    total_b = telemetry.conn.execute(
        "SELECT COUNT(*) FROM forecasts WHERE agent=? AND resolved=1", (b,),
    ).fetchone()[0]
    return {
        "agents": (a, b),
        "n": len(rows),
        "same_price_n": sum(r["a_price"] == r["b_price"] for r in rows),
        "a_brier": sum(r["a_brier"] for r in rows) / len(rows),
        "b_brier": sum(r["b_brier"] for r in rows) / len(rows),
        "unmatched_a": total_a - len(rows),
        "unmatched_b": total_b - len(rows),
    }


def report(telemetry, cfg: dict) -> str:
    agents = list(cfg["agents"])
    lines = ["=== DATA INTEGRITY (diagnostic; locked gate unchanged) ==="]
    for agent in agents:
        d = agent_integrity(telemetry, agent)
        lines.append(
            f"[{agent}] {d['rows']} resolved rows, {d['unique_markets']} unique markets, "
            f"{d['same_day_series_clusters']} same-day series clusters; "
            f"repeat rows={d['repeated_market_rows']}, clustered excess={d['cluster_excess_rows']}"
        )
        sensitivity = cluster_adjusted_paired_brier(telemetry, agent)
        if sensitivity["mean"] is not None and sensitivity["lo"] is not None:
            lines.append(
                f"[{agent}] cluster-adjusted sensitivity only: delta-brier "
                f"{sensitivity['mean']:+.4f} [{sensitivity['lo']:+.4f}, "
                f"{sensitivity['hi']:+.4f}] over {sensitivity['clusters']} clusters"
            )
    matched = matched_head_to_head(telemetry, agents)
    if matched:
        a, b = matched["agents"]
        lines.append(
            f"[matched] n={matched['n']} ({matched['same_price_n']} same benchmark price): "
            f"{a} brier={matched['a_brier']:.4f}, {b} brier={matched['b_brier']:.4f}; "
            f"unmatched resolved rows {a}={matched['unmatched_a']}, {b}={matched['unmatched_b']}"
        )
    lines.append("WARNING: pre-fix repeated/clustered rows remain in the locked gate sample.")
    return "\n".join(lines)
