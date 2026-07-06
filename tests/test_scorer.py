import pytest

from engine import scorer
from engine.telemetry import Telemetry


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes  # market_id -> 'yes'|'no'|None

    def get_market(self, mid):
        res = self.outcomes.get(mid)
        return {"market_id": mid, "status": "settled" if res else "active",
                "result": res or ""}


@pytest.fixture
def tel(tmp_path):
    t = Telemetry(tmp_path / "t.db")
    t.ensure_agent("a", 100.0)
    return t


def test_brier_and_pnl(tel):
    tel.record_forecast(cycle_date="d", agent="a", prompt_version=1, market_id="M1",
                        market_title="t", category="economics", prob=0.8,
                        market_price=0.6, edge_net=0.1, confidence_notes="")
    tel.record_forecast(cycle_date="d", agent="a", prompt_version=1, market_id="M2",
                        market_title="t", category="weather", prob=0.3,
                        market_price=0.5, edge_net=0.1, confidence_notes="")
    # winning yes trade: 10 contracts at 0.60, fees 0.17
    tel.record_trade(cycle_date="d", agent="a", market_id="M1", side="yes",
                     contracts=10, price=0.60, fees=0.17, status="open",
                     paper=1, order_id=None)
    tel.adjust_bankroll("a", -(10 * 0.60 + 0.17))

    stats = scorer.score_resolutions(FakeClient({"M1": "yes", "M2": "no"}), tel)
    assert stats["forecasts_resolved"] == 2
    assert stats["trades_settled"] == 1

    rows = {r["market_id"]: dict(r) for r in
            tel.conn.execute("SELECT * FROM forecasts")}
    assert abs(rows["M1"]["brier"] - (0.8 - 1) ** 2) < 1e-9
    assert abs(rows["M2"]["brier"] - (0.3 - 0) ** 2) < 1e-9

    trade = dict(tel.conn.execute("SELECT * FROM trades").fetchone())
    # gross win = 10*(1-0.60)=4.00, minus fees 0.17
    assert abs(trade["pnl"] - 3.83) < 1e-9
    # bankroll: 100 - 6.17 (entry) + 10.00 (payout) = 103.83
    assert abs(tel.agent_state("a")["bankroll"] - 103.83) < 1e-9


def test_losing_trade(tel):
    tel.record_trade(cycle_date="d", agent="a", market_id="M3", side="no",
                     contracts=5, price=0.40, fees=0.09, status="open",
                     paper=1, order_id=None)
    tel.adjust_bankroll("a", -(5 * 0.40 + 0.09))
    scorer.score_resolutions(FakeClient({"M3": "yes"}), tel)
    trade = dict(tel.conn.execute("SELECT * FROM trades").fetchone())
    assert abs(trade["pnl"] - (-2.09)) < 1e-9
    assert abs(tel.agent_state("a")["bankroll"] - (100 - 2.09)) < 1e-9
