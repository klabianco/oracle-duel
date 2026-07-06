"""Static HTML dashboard generated from telemetry.db."""

from datetime import datetime, timezone

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

    incidents = [dict(r) for r in telemetry.conn.execute(
        "SELECT ts, kind, agent, detail FROM incidents ORDER BY id DESC LIMIT 20")]
    html = f"""<!doctype html><meta charset="utf-8"><title>Oracle Duel</title>
<style>body{{font-family:system-ui;margin:2rem;max-width:70rem}}
table{{border-collapse:collapse;margin:.5rem 0}}
td,th{{border:1px solid #ccc;padding:.25rem .6rem;font-size:.9rem;text-align:left}}</style>
<h1>Oracle Duel</h1>
<p>generated {datetime.now(timezone.utc).isoformat()} ·
mode: {'LIVE' if cfg.get('live') else 'PAPER'}{' (mock)' if cfg.get('mock') else ''}</p>
{''.join(sections)}
<h2>Recent incidents</h2>{_table(incidents, ['ts', 'kind', 'agent', 'detail'])}
"""
    out = STATE_DIR / "dashboard.html"
    out.write_text(html)
    return str(out)
