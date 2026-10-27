"""Paper broker + journal in one SQLite file. Every proposal is logged,
including rejected and expired ones, so you can measure the strategy honestly."""
import json
import sqlite3
import time
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, ts INTEGER, pair TEXT, entry REAL, stop REAL, target REAL,
  qty REAL, reward_risk REAL, risk_usd REAL, setup TEXT, snapshot TEXT,
  llm_verdict TEXT, llm_reason TEXT, status TEXT, decided_ts INTEGER
);
CREATE TABLE IF NOT EXISTS positions (
  id INTEGER PRIMARY KEY, proposal_id INTEGER, pair TEXT, qty REAL, entry REAL,
  stop REAL, target REAL, opened_ts INTEGER, closed_ts INTEGER, exit_price REAL,
  pnl REAL, close_reason TEXT
);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class PaperBroker:
    def __init__(self, path, starting_equity, fee_rate):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.fee = fee_rate
        if self._get("cash") is None:
            self._set("cash", starting_equity)
            self._set("peak", starting_equity)
            self._set("day", _today())
            self._set("day_start_equity", starting_equity)
            self._set("killed", 0)
            self.db.commit()

    # --- key/value state -------------------------------------------------
    def _get(self, key):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return None if row is None else json.loads(row["value"])

    def _set(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    # --- account ---------------------------------------------------------
    def open_positions(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM positions WHERE closed_ts IS NULL")]

    def equity(self, prices):
        """prices: {pair: last_price}. Falls back to entry if a price is missing."""
        held = sum(p["qty"] * prices.get(p["pair"], p["entry"]) for p in self.open_positions())
        return self._get("cash") + held

    def state(self, prices):
        eq = self.equity(prices)
        if self._get("day") != _today():
            self._set("day", _today())
            self._set("day_start_equity", eq)
        if eq > self._get("peak"):
            self._set("peak", eq)
        self.db.commit()
        return {
            "equity": eq, "cash": self._get("cash"), "peak": self._get("peak"),
            "day_start_equity": self._get("day_start_equity"),
            "killed": bool(self._get("killed")),
            "open_pairs": [p["pair"] for p in self.open_positions()],
        }

    def kill(self):
        self._set("killed", 1)
        self.db.commit()

    def reset_kill(self):
        self._set("killed", 0)
        self.db.commit()

    # --- proposals -------------------------------------------------------
    def log_proposal(self, setup, decision, verdict, reason, status):
        cur = self.db.execute(
            "INSERT INTO proposals (ts,pair,entry,stop,target,qty,reward_risk,risk_usd,"
            "setup,snapshot,llm_verdict,llm_reason,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), setup["pair"], setup["entry"], setup["stop"], setup["target"],
             decision.qty, decision.reward_risk, decision.risk_usd, setup["setup"],
             json.dumps(setup["snapshot"]), verdict, reason, status))
        self.db.commit()
        return cur.lastrowid

    def proposal(self, pid):
        row = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return dict(row) if row else None

    def set_status(self, pid, status):
        self.db.execute("UPDATE proposals SET status=?, decided_ts=? WHERE id=?",
                        (status, int(time.time()), pid))
        self.db.commit()

    def pending(self):
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM proposals WHERE status='pending'")]

    # --- fills -----------------------------------------------------------
    def buy(self, pid, price):
        p = self.proposal(pid)
        cash = self._get("cash")
        qty = min(p["qty"], cash / (price * (1 + self.fee)))
        cost = qty * price * (1 + self.fee)
        self._set("cash", cash - cost)
        self.db.execute(
            "INSERT INTO positions (proposal_id,pair,qty,entry,stop,target,opened_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (pid, p["pair"], qty, price, p["stop"], p["target"], int(time.time())))
        self.db.commit()
        return qty

    def sell(self, position_id, price, reason):
        pos = dict(self.db.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone())
        proceeds = pos["qty"] * price * (1 - self.fee)
        pnl = proceeds - pos["qty"] * pos["entry"] * (1 + self.fee)
        self._set("cash", self._get("cash") + proceeds)
        self.db.execute("UPDATE positions SET closed_ts=?, exit_price=?, pnl=?, close_reason=? "
                        "WHERE id=?", (int(time.time()), price, pnl, reason, position_id))
        self.db.commit()
        return pnl

    def check_exits(self, pair, last, high, low, max_hold_min=None, now=None):
        """Spot has no reliable server-side stop via API, so the bot watches levels.
        If both stop and target were touched in the same bar, assume the stop (pessimistic).
        With max_hold_min, a position older than that is sold at the last price."""
        now = now or time.time()
        closed = []
        for pos in self.open_positions():
            if pos["pair"] != pair:
                continue
            if low <= pos["stop"]:
                closed.append((pos, "stop", self.sell(pos["id"], pos["stop"], "stop")))
            elif high >= pos["target"]:
                closed.append((pos, "target", self.sell(pos["id"], pos["target"], "target")))
            elif max_hold_min and now - pos["opened_ts"] >= max_hold_min * 60:
                closed.append((pos, "time", self.sell(pos["id"], last, "time")))
        return closed

    def last_exit_ts(self, pair):
        row = self.db.execute("SELECT MAX(closed_ts) t FROM positions WHERE pair=?",
                              (pair,)).fetchone()
        return row["t"] or 0

    # --- stats -----------------------------------------------------------
    def stats(self):
        rows = [dict(r) for r in self.db.execute(
            "SELECT pnl FROM positions WHERE closed_ts IS NOT NULL")]
        wins = [r["pnl"] for r in rows if r["pnl"] > 0]
        losses = [r["pnl"] for r in rows if r["pnl"] <= 0]
        n = len(rows)
        counts = {r["status"]: r["n"] for r in self.db.execute(
            "SELECT status, COUNT(*) n FROM proposals GROUP BY status")}
        return {
            "closed_trades": n,
            "win_rate": len(wins) / n if n else 0.0,
            "net_pnl": sum(r["pnl"] for r in rows),
            "expectancy": (sum(r["pnl"] for r in rows) / n) if n else 0.0,
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "proposals": counts,
            "by_pair": [dict(r) for r in self.db.execute(
                "SELECT pair, COUNT(*) trades, SUM(pnl > 0) wins, SUM(pnl) pnl "
                "FROM positions WHERE closed_ts IS NOT NULL GROUP BY pair ORDER BY pnl DESC")],
        }
