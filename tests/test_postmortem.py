import json

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


def test_reverted_prompt_does_not_resurrect_failed_mutation(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    tel.record_version("a", 1, runner.prompt_path.read_text(), 4)
    _seed_forecasts(tel, version=1, brier_each=0.10)
    run_postmortem(runner, tel, CFG)  # v2 mutation
    _seed_forecasts(tel, version=2, brier_each=0.30)
    run_postmortem(runner, tel, CFG)  # v3 reverts to v1 text

    _seed_forecasts(tel, version=3, brier_each=0.40)
    out = run_postmortem(runner, tel, CFG)
    v3 = tel.conn.execute(
        "SELECT reverted FROM prompt_versions WHERE agent='a' AND version=3").fetchone()
    assert out["action"] == "mutated" and out["version"] == 4
    assert v3["reverted"] == 0


def test_rejected_mutation_advances_to_a_fresh_round(tmp_path):
    class NoChangeAdapter:
        def complete(self, *args, **kwargs):
            return json.dumps({
                "postmortem": "No safe change.",
                "change_description": "No change.",
                "new_prompt": "Anchor to base rates.\n",
            })

    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    runner.adapter = NoChangeAdapter()
    tel.record_version("a", 1, runner.prompt_path.read_text(), 4)
    _seed_forecasts(tel, version=1, brier_each=0.10)

    out = run_postmortem(runner, tel, CFG)
    assert out["action"] == "kept" and out["version"] == 2
    assert tel.current_version("a")["text"] == "Anchor to base rates.\n"


def test_generation_failure_defers_instead_of_burning_the_round(tmp_path):
    class DownAdapter:
        def complete(self, *args, **kwargs):
            raise RuntimeError("529 overloaded")

    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    runner.adapter = DownAdapter()
    tel.record_version("a", 1, runner.prompt_path.read_text(), 4)
    _seed_forecasts(tel, version=1, brier_each=0.10)

    out = run_postmortem(runner, tel, CFG)
    # an API outage is not a rejected proposal: version unchanged, no
    # postmortem recorded, so tomorrow's cycle retries the same round
    assert out["action"] == "deferred" and out["version"] == 1
    assert tel.current_version("a")["version"] == 1
    assert tel.conn.execute("SELECT COUNT(*) FROM postmortems").fetchone()[0] == 0


def test_unchanged_prompt_round_never_auto_reverts(tmp_path):
    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100)
    runner = StubRunner(tmp_path, tel)
    tel.record_version("a", 1, "Text A.\n", 2)
    tel.close_version_round("a", 1, 0.20, 6, reverted=False)
    runner.deploy_prompt("Text B.\n", 2)              # accepted mutation
    tel.close_version_round("a", 2, 0.15, 6, reverted=False)
    runner.deploy_prompt("Text B.\n", 3)              # rejected round: same text
    _seed_forecasts(tel, version=3, brier_each=0.25)  # worse than v1 — noise

    out = run_postmortem(runner, tel, CFG)
    # v3 made no change, so a bad round must not revert past same-text v2
    # to the historically worse v1 text
    assert out["action"] == "mutated"
    assert "Text A" not in runner.prompt_path.read_text()


def test_rearm_high_water_lowers_only(tmp_path):
    from engine.telemetry import Telemetry
    tel = Telemetry(tmp_path / "t.db")
    tel.ensure_agent("a", 100.0)
    # drawdown reviewed: high-water re-arms at current equity
    tel.rearm_high_water("a", 83.16)
    assert abs(tel.agent_state("a")["high_water"] - 83.16) < 1e-9
    # future gains raise it again via the normal MAX() path
    tel.adjust_bankroll("a", 20.0)
    assert tel.agent_state("a")["high_water"] >= 120.0 - 1e-9
