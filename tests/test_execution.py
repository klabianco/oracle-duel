from engine import risk
from engine.kalshi_client import KalshiClient
from engine.orchestrator import _paper_fill, place_orders
from engine.telemetry import Telemetry


def _order(side="yes", price=0.50, contracts=3):
    return risk.Order(
        agent="a", market_id="M", side=side, contracts=contracts,
        price=price, fees=0.0, edge_net=0.1, prob=0.7,
    )


def test_paper_fill_uses_book_vwap_within_approved_slippage():
    class Client:
        def get_orderbook(self, _):
            return {"no_bids": [(0.49, 1), (0.48, 2)], "yes_bids": []}

    assert _paper_fill(Client(), _order()) == (3, 0.5167)


def test_v2_no_order_normalizes_fill_back_to_no_price():
    client = KalshiClient.__new__(KalshiClient)
    seen = {}

    def request(method, path, params=None, body=None):
        seen.update(method=method, path=path, body=body)
        return {
            "order_id": "O1", "fill_count": "2.00", "remaining_count": "1.00",
            "average_fill_price": "0.5900", "average_fee_paid": "0.0100",
        }

    client._request = request
    result = client.create_order("M", "no", 3, 0.42, client_order_id="C1")
    assert seen["path"] == "/portfolio/events/orders"
    assert seen["body"]["side"] == "ask" and seen["body"]["price"] == "0.5800"
    assert result["filled_count"] == 2
    assert abs(result["average_fill_price"] - 0.41) < 1e-9
    assert abs(result["fees_paid"] - 0.02) < 1e-9


def test_live_accounting_records_only_confirmed_partial_fill(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.orchestrator.stop_flag_set", lambda: False)
    class Client:
        def create_order(self, *args):
            return {
                "order_id": "O1", "filled_count": 2, "remaining_count": 1,
                "average_fill_price": 0.52, "fees_paid": 0.03,
            }

    class Alerts:
        def send(self, *args, **kwargs):
            pass

    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    decision = risk.Decision(orders=[_order(contracts=3)])
    place_orders("a", decision, tel, Client(), {"live": True}, "d", Alerts())

    trade = dict(tel.conn.execute("SELECT * FROM trades").fetchone())
    assert trade["contracts"] == 2 and trade["price"] == 0.52
    assert trade["fees"] == 0.03 and trade["paper"] == 0
    assert abs(tel.agent_state("a")["bankroll"] - 98.93) < 1e-9


def test_paper_fill_takes_partial_fills_like_live_ioc():
    class Client:
        def get_orderbook(self, _):
            # 2.5 + 0.9 = 3.4 contracts executable within slippage
            return {"no_bids": [(0.49, 2.5), (0.48, 0.9)], "yes_bids": []}

    # book absorbs only 3 whole contracts: 5 requested -> partial 3, not None
    assert _paper_fill(Client(), _order(contracts=5)) == (3, 0.5117)
    # float depth must not be truncated per level (int(2.5)+int(0.9)=2 bug):
    # the risk engine approved 3 against 3.4 depth, so 3 must fill
    assert _paper_fill(Client(), _order(contracts=3)) == (3, 0.5117)


def test_live_order_submits_at_the_risk_approved_price(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.orchestrator.stop_flag_set", lambda: False)
    seen = {}

    class Client:
        def create_order(self, ticker, side, count, price):
            seen["price"] = price
            return {"order_id": "O1", "filled_count": count,
                    "average_fill_price": price, "fees_paid": 0.0}

    class Alerts:
        def send(self, *args, **kwargs):
            pass

    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    decision = risk.Decision(orders=[_order(price=0.50)])
    place_orders("a", decision, tel, Client(), {"live": True}, "d", Alerts())
    # the immutable engine sized the position at order.price; a higher IOC
    # limit would let confirmed spend breach the approved position cost
    assert seen["price"] == 0.50
