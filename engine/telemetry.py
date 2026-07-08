"""SQLite telemetry: every forecast, trade, prompt version, postmortem, token dollar and incident."""

import json
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS forecasts (
    id INTEGER PRIMARY KEY,
    cycle_date TEXT NOT NULL,
    agent TEXT NOT NULL,
    prompt_version INTEGER NOT NULL,
    market_id TEXT NOT NULL,
    market_title TEXT,
    category TEXT,
    prob REAL NOT NULL,
    market_price REAL,
    edge_net REAL,
    confidence_notes TEXT,
    resolved INTEGER DEFAULT 0,
    outcome INTEGER,
    brier REAL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    UNIQUE(agent, market_id, cycle_date)
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY,
    cycle_date TEXT NOT NULL,
    agent TEXT NOT NULL,
    market_id TEXT NOT NULL,
    side TEXT NOT NULL,            -- 'yes' | 'no'
    contracts INTEGER NOT NULL,
    price REAL NOT NULL,           -- fill/limit price in dollars per contract
    fees REAL NOT NULL,
    status TEXT NOT NULL,          -- 'open' | 'settled' | 'rejected'
    pnl REAL,                      -- net of fees, set at settlement
    paper INTEGER NOT NULL,        -- 1 = simulated fill, no real order
    order_id TEXT,
    created_at TEXT NOT NULL,
    settled_at TEXT
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    agent TEXT NOT NULL,
    version INTEGER NOT NULL,
    text TEXT NOT NULL,
    word_count INTEGER,
    deployed_at TEXT NOT NULL,
    round_brier REAL,
    round_n INTEGER,
    reverted INTEGER DEFAULT 0,
    PRIMARY KEY (agent, version)
);
CREATE TABLE IF NOT EXISTS postmortems (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    version INTEGER NOT NULL,
    text TEXT,
    proposed_change TEXT,
    accepted INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS token_spend (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    date TEXT NOT NULL,
    phase TEXT NOT NULL,           -- 'research' | 'estimate' | 'postmortem'
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    dollars REAL
);
CREATE TABLE IF NOT EXISTS tool_health (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    date TEXT NOT NULL,
    tool TEXT NOT NULL,
    ok INTEGER NOT NULL,
    err INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    agent TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS agent_state (
    agent TEXT PRIMARY KEY,
    bankroll REAL NOT NULL,
    high_water REAL NOT NULL,
    day TEXT,
    day_start_bankroll REAL,
    halted INTEGER DEFAULT 0,
    halted_reason TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Telemetry:
    def __init__(self, db_path):
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- agent state -------------------------------------------------
    def ensure_agent(self, agent: str, bankroll: float):
        self.conn.execute(
            "INSERT OR IGNORE INTO agent_state(agent, bankroll, high_water) VALUES (?,?,?)",
            (agent, bankroll, bankroll),
        )
        self.conn.commit()

    def agent_state(self, agent: str) -> dict:
        row = self.conn.execute("SELECT * FROM agent_state WHERE agent=?", (agent,)).fetchone()
        return dict(row) if row else None

    def start_day(self, agent: str, day: str):
        """Reset the daily-loss baseline the first time we see a new day."""
        st = self.agent_state(agent)
        if st and st["day"] != day:
            self.conn.execute(
                "UPDATE agent_state SET day=?, day_start_bankroll=bankroll WHERE agent=?",
                (day, agent),
            )
            self.conn.commit()

    def adjust_bankroll(self, agent: str, delta: float):
        self.conn.execute(
            "UPDATE agent_state SET bankroll = bankroll + ?, "
            "high_water = MAX(high_water, bankroll + ?) WHERE agent=?",
            (delta, delta, agent),
        )
        self.conn.commit()

    def set_halted(self, agent: str, halted: bool, reason: str = None):
        self.conn.execute(
            "UPDATE agent_state SET halted=?, halted_reason=? WHERE agent=?",
            (1 if halted else 0, reason, agent),
        )
        self.conn.commit()

    # ---- forecasts ----------------------------------------------------
    def record_forecast(self, **kw):
        self.conn.execute(
            """INSERT OR IGNORE INTO forecasts
               (cycle_date, agent, prompt_version, market_id, market_title, category,
                prob, market_price, edge_net, confidence_notes, created_at)
               VALUES (:cycle_date,:agent,:prompt_version,:market_id,:market_title,
                       :category,:prob,:market_price,:edge_net,:confidence_notes,:created_at)""",
            {**kw, "created_at": _now()},
        )
        self.conn.commit()

    def unresolved_forecasts(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM forecasts WHERE resolved=0")]

    def resolve_forecast(self, forecast_id: int, outcome: int, brier: float):
        self.conn.execute(
            "UPDATE forecasts SET resolved=1, outcome=?, brier=?, resolved_at=? WHERE id=?",
            (outcome, brier, _now(), forecast_id),
        )
        self.conn.commit()

    def resolved_count_for_version(self, agent: str, version: int) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM forecasts WHERE agent=? AND prompt_version=? AND resolved=1",
            (agent, version),
        ).fetchone()[0]

    def version_brier(self, agent: str, version: int):
        row = self.conn.execute(
            "SELECT AVG(brier), COUNT(*) FROM forecasts "
            "WHERE agent=? AND prompt_version=? AND resolved=1",
            (agent, version),
        ).fetchone()
        return row[0], row[1]

    def category_error_log(self, agent: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT category, COUNT(*) n, AVG(brier) brier, AVG(prob) avg_prob, "
            "AVG(outcome) hit_rate FROM forecasts "
            "WHERE agent=? AND resolved=1 GROUP BY category ORDER BY n DESC",
            (agent,),
        ).fetchall()
        return [dict(r) for r in rows]

    def worst_misses(self, agent: str, version: int, limit: int = 5) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM forecasts WHERE agent=? AND prompt_version=? AND resolved=1 "
            "ORDER BY brier DESC LIMIT ?",
            (agent, version, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def calibration_bins(self, agent: str, version: int = None, bins: int = 10) -> list[dict]:
        q = "SELECT prob, outcome FROM forecasts WHERE agent=? AND resolved=1"
        args = [agent]
        if version is not None:
            q += " AND prompt_version=?"
            args.append(version)
        rows = self.conn.execute(q, args).fetchall()
        out = []
        for b in range(bins):
            lo, hi = b / bins, (b + 1) / bins
            sel = [r for r in rows if lo <= r["prob"] < hi or (b == bins - 1 and r["prob"] == 1.0)]
            if sel:
                out.append({
                    "bin": f"{lo:.1f}-{hi:.1f}", "n": len(sel),
                    "mean_prob": sum(r["prob"] for r in sel) / len(sel),
                    "hit_rate": sum(r["outcome"] for r in sel) / len(sel),
                })
        return out

    # ---- trades --------------------------------------------------------
    def record_trade(self, **kw) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (cycle_date, agent, market_id, side, contracts, price,
               fees, status, paper, order_id, created_at)
               VALUES (:cycle_date,:agent,:market_id,:side,:contracts,:price,:fees,
                       :status,:paper,:order_id,:created_at)""",
            {**kw, "created_at": _now()},
        )
        self.conn.commit()
        return cur.lastrowid

    def open_trades(self, agent: str = None) -> list[dict]:
        q, args = "SELECT * FROM trades WHERE status='open'", []
        if agent:
            q += " AND agent=?"
            args.append(agent)
        return [dict(r) for r in self.conn.execute(q, args)]

    def settle_trade(self, trade_id: int, pnl: float):
        self.conn.execute(
            "UPDATE trades SET status='settled', pnl=?, settled_at=? WHERE id=?",
            (pnl, _now(), trade_id),
        )
        self.conn.commit()

    def trades_opened_on(self, agent: str, day: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE agent=? AND cycle_date=? AND status!='rejected'",
            (agent, day),
        ).fetchone()[0]

    def positions_by_market(self, agent: str) -> dict:
        """market_id -> side for open trades (used by the self-match guard)."""
        rows = self.conn.execute(
            "SELECT market_id, side FROM trades WHERE agent=? AND status='open'", (agent,)
        ).fetchall()
        return {r["market_id"]: r["side"] for r in rows}

    def open_stake(self, agent: str) -> float:
        """Cost basis (contracts*price + fees) of the agent's open positions."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(contracts*price+fees),0) FROM trades "
            "WHERE agent=? AND status='open'", (agent,)
        ).fetchone()
        return row[0]

    def total_pnl(self, agent: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM trades WHERE agent=? AND status='settled'", (agent,)
        ).fetchone()
        return row[0]

    # ---- prompt versions ------------------------------------------------
    def current_version(self, agent: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM prompt_versions WHERE agent=? ORDER BY version DESC LIMIT 1", (agent,)
        ).fetchone()
        return dict(row) if row else None

    def record_version(self, agent: str, version: int, text: str, word_count: int):
        self.conn.execute(
            "INSERT INTO prompt_versions(agent, version, text, word_count, deployed_at) "
            "VALUES (?,?,?,?,?)",
            (agent, version, text, word_count, _now()),
        )
        self.conn.commit()

    def close_version_round(self, agent: str, version: int, brier: float, n: int, reverted: bool):
        self.conn.execute(
            "UPDATE prompt_versions SET round_brier=?, round_n=?, reverted=? "
            "WHERE agent=? AND version=?",
            (brier, n, 1 if reverted else 0, agent, version),
        )
        self.conn.commit()

    def record_postmortem(self, agent, version, text, proposed_change, accepted):
        self.conn.execute(
            "INSERT INTO postmortems(agent, version, text, proposed_change, accepted, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (agent, version, text, proposed_change, 1 if accepted else 0, _now()),
        )
        self.conn.commit()

    # ---- spend / incidents ----------------------------------------------
    def record_spend(self, agent, date, phase, model, input_tokens, output_tokens, dollars):
        self.conn.execute(
            "INSERT INTO token_spend(agent, date, phase, model, input_tokens, output_tokens, dollars)"
            " VALUES (?,?,?,?,?,?,?)",
            (agent, date, phase, model, input_tokens, output_tokens, dollars),
        )
        self.conn.commit()

    def spend_on(self, agent: str, date: str) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(dollars),0) FROM token_spend WHERE agent=? AND date=?",
            (agent, date),
        ).fetchone()[0]

    def total_spend(self, agent: str) -> float:
        return self.conn.execute(
            "SELECT COALESCE(SUM(dollars),0) FROM token_spend WHERE agent=?", (agent,)
        ).fetchone()[0]

    def record_tool_health(self, agent: str, date: str, stats: dict):
        """stats: {tool_name: {'ok': n, 'err': n}}"""
        for tool, c in stats.items():
            self.conn.execute(
                "INSERT INTO tool_health(agent, date, tool, ok, err) VALUES (?,?,?,?,?)",
                (agent, date, tool, c.get("ok", 0), c.get("err", 0)),
            )
        self.conn.commit()

    def tool_health_on(self, agent: str, date: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT tool, SUM(ok) ok, SUM(err) err FROM tool_health "
            "WHERE agent=? AND date=? GROUP BY tool", (agent, date)).fetchall()
        return [dict(r) for r in rows]

    def incident(self, kind: str, agent: str = None, detail=None):
        if not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        self.conn.execute(
            "INSERT INTO incidents(ts, kind, agent, detail) VALUES (?,?,?,?)",
            (_now(), kind, agent, detail),
        )
        self.conn.commit()
