from engine.orchestrator import scoreboard
from engine.telemetry import Telemetry

CFG = {"agents": {"claude": {}, "gpt": {}}}


def test_none_until_both_have_a_closed_round(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    tel.record_version("claude", 1, "p", 1)
    tel.close_version_round("claude", 1, 0.15, 100, reverted=False)
    tel.record_version("gpt", 1, "p", 1)   # gpt round not closed yet
    assert scoreboard(tel, CFG) is None


def test_scoreboard_declares_leads(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    for agent, brier in (("claude", 0.12), ("gpt", 0.18)):
        tel.ensure_agent(agent, 100)
        tel.record_version(agent, 1, "p", 1)
        tel.close_version_round(agent, 1, brier, 100, reverted=False)
        tel.record_spend(agent, "d", "estimate", "m", 1000, 100, 2.0)
    # gpt made money, claude lost some
    tel.record_trade(cycle_date="d", agent="gpt", market_id="M", side="yes",
                     contracts=5, price=0.5, fees=0.05, status="open", paper=1,
                     order_id=None)
    tel.settle_trade(1, 2.45)
    tel.record_trade(cycle_date="d", agent="claude", market_id="M2", side="no",
                     contracts=5, price=0.5, fees=0.05, status="open", paper=1,
                     order_id=None)
    tel.settle_trade(2, -2.55)

    sb = scoreboard(tel, CFG)
    assert "calibration lead: claude by 0.0600" in sb
    assert "money lead: gpt by $+5.00" in sb
    assert "round brier 0.1200" in sb and "round brier 0.1800" in sb
    assert "P&L per $" in sb
