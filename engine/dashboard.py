"""Static HTML dashboard generated from telemetry.db."""

import html as html_lib
from datetime import datetime, timezone

from engine import audit_metrics
from engine.config import STATE_DIR


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "<p><em>no data yet</em></p>"
    head = "".join(f"<th>{c}</th>" for c in cols)
    body = ""
    for r in rows:
        cells = "".join(f"<td>{'' if r.get(c) is None else r.get(c)}</td>" for c in cols)
        body += f"<tr>{cells}</tr>"
    return f"<table><tr>{head}</tr>{body}</table>"


def generate(telemetry, cfg: dict) -> str:
    agents = list(cfg["agents"].keys())
    sections = []
    for a in agents:
        st = telemetry.agent_state(a) or {}
        brier_all = telemetry.conn.execute(
            "SELECT AVG(brier), COUNT(*) FROM forecasts WHERE agent=? AND resolved=1", (a,)
        ).fetchone()
        pnl = telemetry.total_pnl(a)
        spend = telemetry.total_spend(a)
        ppd = round(pnl / spend, 3) if spend else None
        versions = [dict(r) for r in telemetry.conn.execute(
            "SELECT version, word_count, deployed_at, round_brier, round_n, reverted "
            "FROM prompt_versions WHERE agent=? ORDER BY version", (a,))]
        cal = telemetry.calibration_bins(a)
        cats = telemetry.category_error_log(a)
        for row in cal + cats + versions:
            for k, v in row.items():
                if isinstance(v, float):
                    row[k] = round(v, 3)
        sections.append(f"""
<h2>{a}</h2>
<p>bankroll <b>${st.get('bankroll', 0):.2f}</b> (high-water ${st.get('high_water', 0):.2f},
halted={bool(st.get('halted'))}) · settled P&amp;L <b>${pnl:.2f}</b> ·
inference spend <b>${spend:.2f}</b> ·
<b>profit per inference dollar: {ppd if ppd is not None else 'n/a'}</b><br>
overall brier <b>{round(brier_all[0], 4) if brier_all[0] is not None else 'n/a'}</b>
over {brier_all[1]} resolved forecasts</p>
<h3>Prompt versions (mutation history)</h3>{_table(versions,
    ['version', 'word_count', 'deployed_at', 'round_brier', 'round_n', 'reverted'])}
<h3>Calibration</h3>{_table(cal, ['bin', 'n', 'mean_prob', 'hit_rate'])}
<h3>Brier by category</h3>{_table(cats, ['category', 'n', 'brier', 'avg_prob', 'hit_rate'])}
""")

    ensemble_html = ""
    if len(agents) >= 2:
        a0, a1 = agents[0], agents[1]
        # Same matched-pair join discipline as audit_metrics.matched_head_to_head:
        # outcomes must agree, and each agent's own benchmark snapshot is used
        # (they are refreshed separately, so neither snapshot alone is "the market").
        pairs = telemetry.conn.execute(
            "SELECT c.prob p0, g.prob p1, c.outcome o, "
            "c.market_price mp0, g.market_price mp1 "
            "FROM forecasts c JOIN forecasts g "
            "ON c.market_id=g.market_id AND c.cycle_date=g.cycle_date "
            "WHERE c.agent=? AND g.agent=? AND c.resolved=1 AND g.resolved=1 "
            "AND c.outcome=g.outcome "
            "AND c.market_price IS NOT NULL AND g.market_price IS NOT NULL",
            (a0, a1)).fetchall()
        if pairs:
            n = len(pairs)
            def _b(key):
                return round(sum((key(r) - r["o"]) ** 2 for r in pairs) / n, 4)
            ensemble_html = f"""<h2>Ensemble (diagnostic — not a gate metric)</h2>
<p>50/50 average of both agents' probabilities on the {n} matched resolved markets:</p>
{_table([
    {'forecaster': a0, 'brier': _b(lambda r: r['p0'])},
    {'forecaster': a1, 'brier': _b(lambda r: r['p1'])},
    {'forecaster': 'ensemble (mean)', 'brier': _b(lambda r: (r['p0'] + r['p1']) / 2)},
    {'forecaster': 'market price (mean of both snapshots)',
     'brier': _b(lambda r: (r['mp0'] + r['mp1']) / 2)},
], ['forecaster', 'brier'])}
<p><em>Errors cancel only if the agents fail differently — watch whether the
ensemble drops below both agents after prompt rounds.</em></p>"""

    pm_html = ""
    pms = [dict(r) for r in telemetry.conn.execute(
        "SELECT agent, version, text, proposed_change, accepted, created_at "
        "FROM postmortems ORDER BY id DESC LIMIT 10")]
    for p in pms:
        status = "accepted" if p["accepted"] else "rejected"
        pm_html += (f"<h4>{p['agent']} · after v{p['version']} · {status} · "
                    f"{p['created_at'][:10]}</h4>"
                    f"<p><b>change:</b> {(p['proposed_change'] or '')[:300]}</p>"
                    f"<blockquote style='white-space:pre-wrap'>"
                    f"{(p['text'] or '')[:2000]}</blockquote>")
    if pm_html:
        pm_html = "<h2>Post-mortems (what the agents said about their own failures)</h2>" + pm_html

    incidents = [dict(r) for r in telemetry.conn.execute(
        "SELECT ts, kind, agent, detail FROM incidents ORDER BY id DESC LIMIT 20")]
    integrity = html_lib.escape(audit_metrics.report(telemetry, cfg))
    html = f"""<!doctype html><meta charset="utf-8"><title>Oracle Duel</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:70rem}}
table{{border-collapse:collapse;margin:.5rem 0}}
td,th{{border:1px solid #ccc;padding:.25rem .6rem;font-size:.9rem;text-align:left}}</style>
<h1>Oracle Duel</h1>
<p>generated {datetime.now(timezone.utc).isoformat()} ·
mode: {'LIVE' if cfg.get('live') else 'PAPER'}{' (mock)' if cfg.get('mock') else ''}</p>
{''.join(sections)}
    {ensemble_html}
    {pm_html}
    <h2>Data integrity</h2><pre>{integrity}</pre>
    <h2>Recent incidents</h2>{_table(incidents, ['ts', 'kind', 'agent', 'detail'])}
"""
    out = STATE_DIR / "dashboard.html"
    out.write_text(html)
    return str(out)
