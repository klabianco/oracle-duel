from datetime import datetime, timezone

from engine.orchestrator import plain_digest
from engine.telemetry import Telemetry

CFG = {"agents": {"claude": {}, "gpt": {}}, "bankroll_start": 100.0}
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _forecast_kw(agent, mid, prob):
    return dict(cycle_date=TODAY, agent=agent, prompt_version=1, market_id=mid,
                market_title=f"Market {mid}", category="test", prob=prob,
                market_price=0.5, edge_net=0.0, confidence_notes=None)


def _telemetry(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    for agent in CFG["agents"]:
        tel.ensure_agent(agent, 100.0)
    return tel


def test_resolved_count_is_per_market_not_divided_by_agents(tmp_path):
    # gpt has a resolved forecast on a market claude never forecast
    # (e.g. claude's telemetry was reset) — the count must still say 1, not 0
    tel = _telemetry(tmp_path)
    tel.record_forecast(**_forecast_kw("gpt", "M1", 0.6))
    fid = tel.conn.execute("SELECT id FROM forecasts").fetchone()["id"]
    tel.resolve_forecast(fid, outcome=0, brier=0.36)

    digest = plain_digest(tel, CFG, TODAY, 10, {"claude": [], "gpt": []})
    assert "1 old guess got their answers today" in digest
    assert "gpt called 0 of 1 right" in digest


def test_resolved_count_dedupes_shared_markets(tmp_path):
    # both agents forecast the same market: one market resolved, not two
    tel = _telemetry(tmp_path)
    for agent in ("claude", "gpt"):
        tel.record_forecast(**_forecast_kw(agent, "M1", 0.7))
    for row in tel.conn.execute("SELECT id FROM forecasts").fetchall():
        tel.resolve_forecast(row["id"], outcome=1, brier=0.09)

    digest = plain_digest(tel, CFG, TODAY, 10, {"claude": [], "gpt": []})
    assert "1 old guess got their answers today" in digest
    assert "claude called 1 of 1 right" in digest
    assert "gpt called 1 of 1 right" in digest


def test_halted_agent_digest_says_paused_not_no_edge(tmp_path):
    tel = _telemetry(tmp_path)
    tel.set_halted("claude", True, "circuit breaker: test")

    digest = plain_digest(tel, CFG, TODAY, 10, {"claude": [], "gpt": []})
    assert "Placed no bets — betting is paused." in digest
    # only the un-halted agent (gpt) gets the no-edge explanation
    assert digest.count("No idea was strong enough") == 1
    assert "PAUSED" in digest
