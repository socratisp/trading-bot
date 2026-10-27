import copy
import math
import time

import pytest
import yaml

from bot import indicators, llm, risk, strategy
from bot.main import Bot
from bot.paper import PaperBroker

CFG = yaml.safe_load(open("config.yaml"))


def make_candles(n=300, interval=240, trend=0.002, wobble=0.03, base=100.0):
    """Uptrend with a sine wobble; ends on a pullback."""
    out, start = [], int(time.time() * 1000) - (n + 2) * interval * 60_000
    price = base
    for i in range(n):
        drift = price * trend
        price = base * math.exp(trend * i) * (1 + wobble * math.sin(i / 6))
        out.append({"start": start + i * interval * 60_000, "open": price - drift,
                    "high": price * 1.01, "low": price * 0.99, "close": price, "volume": 1.0})
    return out


# --- indicators -----------------------------------------------------------
def test_rsi_extremes():
    assert indicators.rsi(list(range(1, 40))) == 100.0
    assert indicators.rsi(list(range(40, 1, -1))) < 1


def test_atr_constant_range():
    highs, lows, closes = [11] * 30, [9] * 30, [10] * 30
    assert indicators.atr(highs, lows, closes, 14) == pytest.approx(2.0)


def test_ema_flat():
    assert indicators.ema([5.0] * 60, 50)[-1] == pytest.approx(5.0)


# --- risk -------------------------------------------------------------------
def base_state(**kw):
    s = {"equity": 200.0, "cash": 200.0, "peak": 200.0, "day_start_equity": 200.0,
         "killed": False, "open_pairs": []}
    s.update(kw)
    return s


def setup(entry=100.0, stop=95.0, target=115.0, pair="BTC-USD"):
    return {"pair": pair, "entry": entry, "stop": stop, "target": target}


def test_sizing_risks_one_percent():
    d = risk.check(setup(), base_state(), CFG)
    assert d.ok
    # 1% of 200 = 2 USD at risk; 5 USD stop distance -> 0.4 units (cap 30% = 0.6)
    assert d.qty == pytest.approx(0.4)
    assert 2.0 <= d.risk_usd <= 2.3  # plus fees


def test_position_cap():
    d = risk.check(setup(stop=99.5, target=103), base_state(), CFG)
    assert d.ok
    assert d.qty * 100 <= 200 * 0.30 + 1e-9


def test_rejects_bad_reward_risk():
    d = risk.check(setup(target=104), base_state(), CFG)
    assert not d.ok and "reward:risk" in d.reason


def test_daily_halt():
    d = risk.check(setup(), base_state(equity=193.0), CFG)
    assert not d.ok and "daily loss" in d.reason


def test_kill_switch_drawdown():
    d = risk.check(setup(), base_state(equity=179.0, peak=200.0, day_start_equity=179.0), CFG)
    assert not d.ok and "drawdown" in d.reason


def test_max_positions_and_duplicates():
    assert not risk.check(setup(), base_state(open_pairs=["ETH-USD", "SOL-USD"]), CFG).ok
    assert not risk.check(setup(), base_state(open_pairs=["BTC-USD"]), CFG).ok


def test_killed_flag():
    assert not risk.check(setup(), base_state(killed=True), CFG).ok


# --- strategy ---------------------------------------------------------------
def test_strategy_levels_are_atr_based():
    candles = make_candles()
    found = None
    for end in range(220, len(candles)):
        found = strategy.find_setup("BTC-USD", candles[:end], CFG["strategy"])
        if found:
            break
    assert found, "expected at least one pullback setup in a wobbling uptrend"
    a = found["snapshot"]["atr"]
    assert found["entry"] - found["stop"] == pytest.approx(1.5 * a)
    assert found["target"] - found["entry"] == pytest.approx(3.5 * a)


def test_no_setup_in_downtrend():
    candles = make_candles(trend=-0.002)
    assert all(strategy.find_setup("BTC-USD", candles[:e], CFG["strategy"]) is None
               for e in range(220, 300))


# --- llm parsing ------------------------------------------------------------
def test_llm_parse():
    out = '{"type":"result","result":"```json\\n{\\"verdict\\": \\"veto\\", \\"reason\\": \\"hack\\"}\\n```"}'
    assert llm.parse(out) == ("veto", "hack")
    assert llm.parse("garbage")[0] == "skipped"
    assert llm.parse('{"result":"{\\"verdict\\":\\"buy now\\"}"}')[0] == "skipped"


# --- paper broker -----------------------------------------------------------
def test_paper_round_trip(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 200, 0.001)
    d = risk.Decision(True, "ok", qty=0.4, risk_usd=2.2, reward_risk=2.8)
    s = {**setup(), "setup": "test", "snapshot": {}}
    pid = b.log_proposal(s, d, "approve", "fine", "pending")
    b.buy(pid, 100.0)
    assert b.state({"BTC-USD": 100.0})["cash"] == pytest.approx(200 - 40.04)
    closed = b.check_exits("BTC-USD", 94, 101, 94)  # stop touched
    assert closed[0][1] == "stop"
    assert closed[0][2] == pytest.approx(0.4 * 95 * 0.999 - 40.04)
    assert b.stats()["closed_trades"] == 1


def test_stop_wins_when_both_levels_hit(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 200, 0.0)
    d = risk.Decision(True, "ok", qty=0.4)
    pid = b.log_proposal({**setup(), "setup": "", "snapshot": {}}, d, None, "", "pending")
    b.buy(pid, 100.0)
    assert b.check_exits("BTC-USD", 100, 120, 90)[0][1] == "stop"


# --- full cycle, offline ------------------------------------------------------
class FakeMarket:
    def __init__(self, candles):
        self.c = candles
        self.price = candles[-1]["close"]
        self.hi = self.lo = self.price

    def candles(self, pair, interval):
        return self.c

    def last_price(self, pair):
        return self.price, self.hi, self.lo


class FakeTG:
    def __init__(self):
        self.sent, self.queue = [], []

    def send(self, text, buttons=None):
        self.sent.append((text, buttons))

    def updates(self):
        q, self.queue = self.queue, []
        return q


def pullback_candles():
    c = make_candles()
    for end in range(220, len(c)):
        if strategy.find_setup("BTC-USD", c[:end], CFG["strategy"]):
            return c[:end]
    raise AssertionError("no setup")


def make_bot(tmp_path, verdict="approve", manual=True):
    cfg = copy.deepcopy(CFG)
    cfg["approval"]["required"] = manual
    cfg["data_dir"] = str(tmp_path)
    cfg["exchange"]["pairs"] = ["BTC-USD"]
    tg, mkt = FakeTG(), FakeMarket(pullback_candles())
    bot = Bot(cfg, market=mkt, tg=tg, reviewer=lambda *a: (verdict, "test"),
              headlines=lambda: ["nothing"])
    return bot, tg, mkt


def test_cycle_propose_approve_target(tmp_path):
    bot, tg, mkt = make_bot(tmp_path)
    bot.scan()
    text, buttons = tg.sent[-1]
    assert "Paper trade #1" in text and buttons[0]["callback_data"] == "ok:1"
    bot.scan()  # same bar -> no duplicate proposal
    assert len(bot.broker.pending()) == 1

    tg.queue = [("button", "ok:1")]
    bot.handle_telegram()
    assert bot.broker.proposal(1)["status"] == "filled"
    pos = bot.broker.open_positions()[0]

    mkt.price = mkt.hi = pos["target"] + 1
    mkt.lo = pos["entry"]
    bot.monitor()
    assert bot.broker.stats()["closed_trades"] == 1
    assert bot.broker.stats()["net_pnl"] > 0


def test_cycle_veto_and_expiry(tmp_path):
    bot, tg, _ = make_bot(tmp_path, verdict="veto")
    bot.scan()
    assert "vetoed" in tg.sent[-1][0]
    assert bot.broker.pending() == []

    bot2, tg2, _ = make_bot(tmp_path / "b")
    bot2.scan()
    bot2.broker.db.execute("UPDATE proposals SET ts = ts - 3600")
    bot2.expire()
    assert bot2.broker.proposal(1)["status"] == "expired"
    tg2.queue = [("button", "ok:1")]
    bot2.handle_telegram()
    assert bot2.broker.open_positions() == []


def test_kill_switch_triggers(tmp_path):
    bot, tg, mkt = make_bot(tmp_path)
    bot.scan()
    tg.queue = [("button", "ok:1")]
    bot.handle_telegram()
    # Simulate the account having peaked much higher.
    bot.broker._set("peak", 1000)
    bot.broker.db.commit()
    bot.monitor()
    assert "Kill switch" in tg.sent[-1][0]
    assert bot.broker.state({})["killed"]


def test_auto_approve_fills_immediately(tmp_path):
    bot, tg, _ = make_bot(tmp_path, manual=False)
    bot.scan()
    assert bot.broker.proposal(1)["status"] == "filled"
    assert len(bot.broker.open_positions()) == 1
    assert "paper BUY" in tg.sent[-1][0]
