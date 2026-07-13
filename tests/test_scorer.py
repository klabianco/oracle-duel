import pytest

from engine import scorer
from engine.telemetry import Telemetry


class FakeClient:
    def __init__(self, outcomes):
        # market_id -> 'yes'|'no'|None, or a full market dict for special cases
        self.outcomes = outcomes

    def get_market(self, mid):
        res = self.outcomes.get(mid)
        if isinstance(res, dict):
            return {"market_id": mid, **res}
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


def test_scalar_settlement_voids_forecast_and_settles_trades(tel):
    # Kalshi "fair price" settlement (e.g. cancelled game): result='scalar'
    scalar_market = {"status": "finalized", "result": "scalar",
                     "settlement_value": 0.89}
    tel.record_forecast(cycle_date="d", agent="a", prompt_version=1, market_id="M4",
                        market_title="t", category="sports", prob=0.08,
                        market_price=0.59, edge_net=0.1, confidence_notes="")
    # NO trade: 7 contracts at 0.45, fees 0.10 -> each NO pays 1-0.89=0.11
    tel.record_trade(cycle_date="d", agent="a", market_id="M4", side="no",
                     contracts=7, price=0.45, fees=0.10, status="open",
                     paper=1, order_id=None)
    tel.adjust_bankroll("a", -(7 * 0.45 + 0.10))

    stats = scorer.score_resolutions(FakeClient({"M4": scalar_market}), tel)
    assert stats["forecasts_voided"] == 1
    assert stats["forecasts_resolved"] == 0
    assert stats["trades_settled"] == 1

    row = dict(tel.conn.execute("SELECT * FROM forecasts").fetchone())
    assert row["resolved"] == 2  # voided: excluded from resolved=1 Brier queries
    assert row["brier"] is None
    assert "scalar" in row["void_reason"]

    trade = dict(tel.conn.execute("SELECT * FROM trades").fetchone())
    # pnl = 7*(0.11-0.45) - 0.10 = -2.48
    assert abs(trade["pnl"] - (-2.48)) < 1e-9
    # bankroll: 100 - 3.25 (entry) + 7*0.11 (payout) = 97.52
    assert abs(tel.agent_state("a")["bankroll"] - 97.52) < 1e-9
    # voided rows never re-enter the unresolved queue
    assert tel.unresolved_forecasts() == []


def test_settled_with_empty_result_is_not_scalar(tel):
    # A settled market can transiently report result='' — possibly with a
    # default settlement_value of 0. It must stay unresolved, NOT be voided
    # or settled at $0 (both are irreversible).
    transient = {"status": "finalized", "result": "", "settlement_value": 0.0}
    tel.record_forecast(cycle_date="d", agent="a", prompt_version=1, market_id="M5",
                        market_title="t", category="sports", prob=0.6,
                        market_price=0.5, edge_net=0.1, confidence_notes="")
    tel.record_trade(cycle_date="d", agent="a", market_id="M5", side="yes",
                     contracts=10, price=0.50, fees=0.10, status="open",
                     paper=1, order_id=None)

    stats = scorer.score_resolutions(FakeClient({"M5": transient}), tel)
    assert stats["forecasts_resolved"] == 0
    assert stats["forecasts_voided"] == 0
    assert stats["trades_settled"] == 0
    assert len(tel.unresolved_forecasts()) == 1
    trade = dict(tel.conn.execute("SELECT * FROM trades").fetchone())
    assert trade["status"] == "open" and trade["pnl"] is None
