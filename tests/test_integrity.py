from engine import audit_metrics
from datetime import datetime, timezone

from engine.agent_runner import AgentRunner
from engine.orchestrator import _cycle_date
from engine.telemetry import Telemetry


def _forecast(agent, day, market, event, prob=0.6, price=0.5):
    return dict(
        cycle_date=day, agent=agent, prompt_version=1, market_id=market,
        market_title=market, category="test", prob=prob, market_price=price,
        edge_net=0.0, confidence_notes="", event_ticker=event,
        series_ticker=event, snapshot_at="2026-07-10T14:00:00+00:00",
        yes_bid=0.49, yes_ask=0.51, no_bid=0.49, no_ask=0.51,
    )


def test_market_mid_keeps_the_historical_locked_gate_benchmark():
    # zero/absent bid falls back to the ask — the formula every historical
    # forecast row used; changing it would redefine the locked gate mid-sample
    assert AgentRunner._market_mid({"yes_bid": 0.0, "yes_ask": 0.10}) == 0.10
    assert AgentRunner._market_mid({"yes_bid": None, "yes_ask": 0.10}) == 0.10
    assert AgentRunner._market_mid({"yes_bid": 0.40, "yes_ask": 0.50}) == 0.45


def test_cycle_date_uses_operating_timezone(monkeypatch):
    class Client:
        def now(self):
            return datetime(2026, 7, 11, 0, 30, tzinfo=timezone.utc)

    monkeypatch.delenv("ORACLE_CYCLE_DATE", raising=False)
    assert _cycle_date(Client(), {"timezone": "America/Los_Angeles"}) == "2026-07-10"


def test_cycle_claim_and_forecast_insert_are_atomic(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    assert tel.claim_cycle("2026-07-11") is True
    assert tel.claim_cycle("2026-07-11") is False
    first = tel.record_forecast(**_forecast("a", "2026-07-11", "M1", "E1"))
    duplicate = tel.record_forecast(**_forecast("a", "2026-07-11", "M1", "E1"))
    assert first is not None and duplicate is None


def test_integrity_report_exposes_clusters_and_unmatched_rows(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    for agent in ("claude", "gpt"):
        for market in ("M1", "M2"):
            fid = tel.record_forecast(**_forecast(agent, "d", market, "EVENT"))
            tel.resolve_forecast(fid, 1, 0.16)
    fid = tel.record_forecast(**_forecast("gpt", "d", "M3", "OTHER"))
    tel.resolve_forecast(fid, 0, 0.36)

    report = audit_metrics.report(tel, {"agents": {"claude": {}, "gpt": {}}})
    assert "clustered excess=1" in report
    assert "cluster-adjusted sensitivity only" in report
    assert "unmatched resolved rows claude=0, gpt=1" in report
