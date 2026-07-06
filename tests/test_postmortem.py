from engine.adapters import MockAdapter
from engine.postmortem import run_postmortem
from engine.telemetry import Telemetry

CFG = {"round": {"min_days": 7, "min_resolved_forecasts": 5, "prompt_word_cap": 500}}


class StubRunner:
    def __init__(self, tmp_path, telemetry):
        self.name = "a"
        self.telemetry = telemetry
        self.prompt_path = tmp_path / "prompt.md"
        self.prompt_path.write_text("Anchor to base rates.\n")
        self.adapter = MockAdapter("mock", {"mock": {"input": 0, "output": 0}})

    def deploy_prompt(self, text, version):
        self.prompt_path.write_text(text)
        self.telemetry.record_version("a", version, text, len(text.split()))

    def _flush_spend(self, *a, **k):
        pass


def _seed_forecasts(tel, version, brier_each, n=6):
    for i in range(n):
        tel.record_forecast(cycle_date=f"d{version}", agent="a", prompt_version=version,
                            market_id=f"V{version}M{i}", market_title="t",
                            category="economics", prob=0.5, market_price=0.5,
                            edge_net=0, confidence_notes="")
    for row in tel.conn.execute(
            "SELECT id FROM forecasts WHERE prompt_version=? AND resolved=0", (version,)):
        tel.conn.execute("UPDATE forecasts SET resolved=1, outcome=1, brier=? WHERE id=?",
                         (brier_each, row["id"]))
    tel.conn.commit()


def test_mutation_deploys_new_version(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    tel.record_version("a", 1, runner.prompt_path.read_text(), 4)
    _seed_forecasts(tel, version=1, brier_each=0.20)

    out = run_postmortem(runner, tel, CFG)
    assert out["action"] == "mutated" and out["version"] == 2
    assert "Mock lesson" in runner.prompt_path.read_text()
    v1 = tel.conn.execute(
        "SELECT round_brier FROM prompt_versions WHERE agent='a' AND version=1").fetchone()
    assert abs(v1["round_brier"] - 0.20) < 1e-9


def test_auto_revert_when_mutation_hurts(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    v1_text = runner.prompt_path.read_text()
    tel.record_version("a", 1, v1_text, 4)
    _seed_forecasts(tel, version=1, brier_each=0.10)
    run_postmortem(runner, tel, CFG)          # closes v1 (brier 0.10), deploys v2

    _seed_forecasts(tel, version=2, brier_each=0.30)   # v2 scores WORSE
    out = run_postmortem(runner, tel, CFG)
    assert out["action"] == "reverted" and out["version"] == 3
    assert runner.prompt_path.read_text() == v1_text   # back to v1 text
    v2 = tel.conn.execute(
        "SELECT reverted FROM prompt_versions WHERE agent='a' AND version=2").fetchone()
    assert v2["reverted"] == 1
