from engine import gates
from engine.telemetry import Telemetry


def _seed(tel, agent, rows):
    """rows: (prob, market_price, outcome, edge_net)"""
    for i, (prob, mp, outcome, edge) in enumerate(rows):
        tel.record_forecast(cycle_date=f"d{i}", agent=agent, prompt_version=1,
                            market_id=f"M{i}", market_title="t", category="c",
                            prob=prob, market_price=mp, edge_net=edge,
                            confidence_notes="")
    for row in tel.conn.execute(
            "SELECT id, market_id FROM forecasts WHERE agent=?", (agent,)):
        i = int(row["market_id"][1:])
        tel.conn.execute("UPDATE forecasts SET resolved=1, outcome=?, "
                         "brier=(prob-?)*(prob-?) WHERE id=?",
                         (rows[i][2], rows[i][2], rows[i][2], row["id"]))
    tel.conn.commit()


def test_paired_brier_favors_better_forecaster(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    # agent consistently closer to the truth than the market
    _seed(tel, "a", [(0.9, 0.6, 1, 0.0)] * 50 + [(0.1, 0.4, 0, 0.0)] * 50)
    pb = gates.paired_brier(tel, "a")
    assert pb["n"] == 100
    assert pb["mean"] > 0 and pb["lo"] > 0


def test_realized_claim_edge_sides(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    # yes-side claim that hit, no-side claim that hit, yes-side claim that missed
    _seed(tel, "a", [
        (0.80, 0.50, 1, 0.25),   # +0.5
        (0.10, 0.60, 0, 0.25),   # +0.6
        (0.80, 0.50, 0, 0.25),   # -0.5
        (0.50, 0.50, 1, 0.00),   # below claim threshold — excluded
    ])
    ce = gates.realized_claim_edge(tel, "a")
    assert ce["n"] == 3
    assert abs(ce["mean"] - (0.5 + 0.6 - 0.5) / 3) < 1e-9


def test_verdict_insufficient_then_kill_then_go():
    small = {"n": 50, "mean": 0.0, "lo": -0.01, "hi": 0.01, "se": 0.005}
    assert "INSUFFICIENT" in gates.verdict(small, {"n": 0, "mean": None, "se": None})

    dead = {"n": 400, "mean": 0.0005, "lo": -0.002, "hi": 0.003, "se": 0.001}
    no_claims = {"n": 4, "mean": None, "lo": None, "hi": None, "se": None}
    assert "KILL" in gates.verdict(dead, no_claims)

    good = {"n": 400, "mean": 0.012, "lo": 0.004, "hi": 0.020, "se": 0.004}
    assert "GO" in gates.verdict(good, no_claims)

    ambiguous = {"n": 400, "mean": 0.004, "lo": -0.002, "hi": 0.010, "se": 0.003}
    assert "AMBIGUOUS" in gates.verdict(ambiguous, no_claims)

    stuck = {"n": 650, "mean": 0.004, "lo": -0.002, "hi": 0.010, "se": 0.003}
    assert "HARD STOP" in gates.verdict(stuck, no_claims)
