import copy
import math
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from bot import indicators, llm, risk, strategy
from bot.main import Bot
from bot.paper import PaperBroker

CFG = yaml.safe_load(open("config.yaml"))
R = CFG["risk"]
FEE = CFG["exchange"]["fee_rate"]
IND = CFG["indicators"]
S = CFG["strategies"]


def make_candles(n=300, interval=240, trend=0.002, wobble=0.03, base=100.0):
    """Trend with a sine wobble."""
    out, start = [], int(time.time() * 1000) - (n + 2) * interval * 60_000
    for i in range(n):
        price = base * math.exp(trend * i) * (1 + wobble * math.sin(i / 6))
        out.append({"start": start + i * interval * 60_000, "open": price,
                    "high": price * 1.01, "low": price * 0.99, "close": price, "volume": 1.0})
    return out


def crash_then_tick(n=120, interval=60):
    """Steady fall (RSI very low), then one candle that closes higher."""
    out, start = [], int(time.time() * 1000) - (n + 2) * interval * 60_000
    price = 100.0
    for i in range(n):
        price = price * (0.99 if i < n - 1 else 1.003)
        out.append({"start": start + i * interval * 60_000, "open": price,
                    "high": price * 1.004, "low": price * 0.996, "close": price, "volume": 1.0})
    return out


def noisy_uptrend(seed=1, drift=0.001, n=400, interval=15):
    import random
    rnd, out, p = random.Random(seed), [], 100.0
    start = int(time.time() * 1000) - (n + 2) * interval * 60_000
    for i in range(n):
        p *= 1 + drift + rnd.gauss(0, 0.006)
        out.append({"start": start + i * interval * 60_000, "open": p, "high": p * 1.003,
                    "low": p * 0.997, "close": p, "volume": 1.0})
    return out


def first_setup(candles, s, start=220):
    for end in range(start, len(candles)):
        if strategy.find_setup("BTC-USD", candles[:end], s, IND):
            return candles[:end]
    raise AssertionError("no setup found")


# --- indicators -----------------------------------------------------------
def test_rsi_extremes():
    assert indicators.rsi(list(range(1, 40))) == 100.0
    assert indicators.rsi(list(range(40, 1, -1))) < 1


def test_atr_constant_range():
    assert indicators.atr([11] * 30, [9] * 30, [10] * 30, 14) == pytest.approx(2.0)


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
    d = risk.check(setup(), base_state(), R, FEE)
    assert d.ok
    assert d.qty == pytest.approx(0.4)  # 2 USD risk / 5 USD stop distance
    assert 2.0 <= d.risk_usd <= 2.1


def test_position_cap():
    d = risk.check(setup(stop=99.5, target=103), base_state(), R, FEE)
    assert d.ok and d.qty * 100 <= 200 * 0.30 + 1e-9


def test_reward_risk_rule_and_override():
    assert not risk.check(setup(target=107), base_state(), R, FEE).ok      # ~1.3 < 2.0
    assert risk.check(setup(target=107), base_state(), R, FEE, min_rr=1.2).ok


def test_daily_halt_and_drawdown():
    assert "daily loss" in risk.check(setup(), base_state(equity=193.0), R, FEE).reason
    d = risk.check(setup(), base_state(equity=179.0, day_start_equity=179.0), R, FEE)
    assert "drawdown" in d.reason


def test_positions_duplicates_killed():
    assert not risk.check(setup(), base_state(open_pairs=["ETH-USD", "SOL-USD"]), R, FEE).ok
    assert not risk.check(setup(), base_state(open_pairs=["BTC-USD"]), R, FEE).ok
    assert not risk.check(setup(), base_state(killed=True), R, FEE).ok


# --- strategies ---------------------------------------------------------------
def test_trend_levels_are_atr_based():
    found = strategy.find_setup("BTC-USD", first_setup(make_candles(), S["trend_4h"]),
                                S["trend_4h"], IND)
    a = found["snapshot"]["atr"]
    assert found["entry"] - found["stop"] == pytest.approx(1.5 * a)
    assert found["target"] - found["entry"] == pytest.approx(3.5 * a)


def test_trend_silent_in_downtrend():
    c = make_candles(trend=-0.002)
    assert all(strategy.find_setup("BTC-USD", c[:e], S["trend_4h"], IND) is None
               for e in range(220, 300))


def test_bounce_fires_after_crash_and_uptick():
    c = crash_then_tick()
    found = strategy.find_setup("BTC-USD", c, S["bounce_1h"], IND)
    assert found and "Oversold" in found["setup"]
    assert strategy.find_setup("BTC-USD", c[:-1], S["bounce_1h"], IND) is None  # no uptick yet
    assert strategy.find_setup("BTC-USD", c, S["trend_1h"], IND) is None        # trend stays out


# --- llm parsing ------------------------------------------------------------
def test_llm_parse():
    out = '{"type":"result","result":"```json\\n{\\"verdict\\": \\"veto\\", \\"reason\\": \\"hack\\"}\\n```"}'
    assert llm.parse(out) == ("veto", "hack")
    assert llm.parse("garbage")[0] == "skipped"
    assert llm.parse('{"result":"{\\"verdict\\":\\"buy now\\"}"}')[0] == "skipped"


# --- paper broker -----------------------------------------------------------
def opened(b, pair="BTC-USD", qty=0.4, price=100.0, **lv):
    d = risk.Decision(True, "ok", qty=qty)
    pid = b.log_proposal({**setup(pair=pair, **lv), "setup": "", "snapshot": {}}, d, None, "", "filled")
    b.buy(pid, price)
    return b.open_positions()[-1]


def test_paper_round_trip_with_fees(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 200, 0.001)
    opened(b)
    assert b.state({"BTC-USD": 100.0})["cash"] == pytest.approx(200 - 40.04)
    closed = b.check_exits("BTC-USD", 94, 101, 94)
    assert closed[0][1] == "stop"
    assert closed[0][2] == pytest.approx(0.4 * 95 * 0.999 - 40.04)


def test_stop_wins_when_both_levels_hit(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 200, 0.0)
    opened(b)
    assert b.check_exits("BTC-USD", 100, 120, 90)[0][1] == "stop"


def test_time_exit_and_cooldown_clock(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 200, 0.0)
    pos = opened(b)
    assert b.check_exits("BTC-USD", 101, 101, 99, max_hold_min=20,
                         now=pos["opened_ts"] + 19 * 60) == []
    closed = b.check_exits("BTC-USD", 101, 101, 99, max_hold_min=20,
                           now=pos["opened_ts"] + 21 * 60)
    assert closed[0][1] == "time" and closed[0][2] == pytest.approx(0.4)
    assert time.time() - b.last_exit_ts("BTC-USD") < 5
    assert b.last_exit_ts("ETH-USD") == 0


def test_stats_by_pair(tmp_path):
    b = PaperBroker(str(tmp_path / "t.sqlite"), 1000, 0.0)
    for pair, exit_price in [("BTC-USD", 120), ("SOL-USD", 95), ("BTC-USD", 95)]:
        b.sell(opened(b, pair=pair, qty=1.0)["id"], exit_price, "test")
    rows = {r["pair"]: r for r in b.stats()["by_pair"]}
    assert rows["BTC-USD"]["trades"] == 2 and rows["BTC-USD"]["wins"] == 1
    assert rows["BTC-USD"]["pnl"] == pytest.approx(15.0)
    assert rows["SOL-USD"]["pnl"] == pytest.approx(-5.0)


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

    def send_photo(self, png, caption=""):
        assert png[:4] == b"\x89PNG"
        self.sent.append((caption, "photo"))

    def updates(self):
        q, self.queue = self.queue, []
        return q


def make_bot(tmp_path, only=("trend_4h",), verdict="approve", manual=True, candles=None):
    cfg = copy.deepcopy(CFG)
    cfg["approval"]["required"] = manual
    cfg["data_dir"] = str(tmp_path)
    cfg["exchange"]["pairs"] = ["BTC-USD"]
    cfg["exchange"]["min_volume_usd_24h"] = 0
    cfg["strategies"] = {k: v for k, v in cfg["strategies"].items() if k in only}
    tg = FakeTG()
    mkt = FakeMarket(candles or first_setup(make_candles(), S["trend_4h"]))
    bot = Bot(cfg, market=mkt, tg=tg, reviewer=lambda *a: (verdict, "test"),
              headlines=lambda: ["nothing"])
    return bot, tg, mkt


def test_cycle_propose_approve_target(tmp_path):
    bot, tg, mkt = make_bot(tmp_path)
    run = bot.runners["trend_4h"]
    bot.scan(run)
    text, buttons = tg.sent[-1]
    assert text.startswith("[trend_4h]") and "Paper trade #1" in text
    assert buttons[0]["callback_data"] == "ok:trend_4h:1"
    bot.scan(run)  # same bar -> no duplicate
    assert len(run.broker.pending()) == 1

    tg.queue = [("button", "ok:trend_4h:1")]
    bot.handle_telegram()
    assert run.broker.proposal(1)["status"] == "filled"
    pos = run.broker.open_positions()[0]
    mkt.price = mkt.hi = pos["target"] + 1
    mkt.lo = pos["entry"]
    bot.monitor()
    assert run.broker.stats()["closed_trades"] == 1 and run.broker.stats()["net_pnl"] > 0


def test_veto_and_expiry(tmp_path):
    bot, tg, _ = make_bot(tmp_path, verdict="veto")
    bot.scan(bot.runners["trend_4h"])
    assert "vetoed" in tg.sent[-1][0]

    bot2, tg2, _ = make_bot(tmp_path / "b")
    run = bot2.runners["trend_4h"]
    bot2.scan(run)
    run.broker.db.execute("UPDATE proposals SET ts = ts - 3600")
    bot2.expire()
    assert run.broker.proposal(1)["status"] == "expired"
    tg2.queue = [("button", "ok:trend_4h:1")]
    bot2.handle_telegram()
    assert run.broker.open_positions() == []


def test_llm_only_where_enabled(tmp_path):
    calls = []
    bot, tg, _ = make_bot(tmp_path, only=("bounce_1h",), manual=False, candles=crash_then_tick())
    bot.reviewer = lambda *a: calls.append(1) or ("veto", "x")
    bot.scan(bot.runners["bounce_1h"])
    assert calls == []  # bounce has llm_review: false
    assert bot.runners["bounce_1h"].broker.open_positions()


def test_strategies_have_separate_accounts(tmp_path):
    c = first_setup(make_candles(), S["trend_4h"])
    bot, tg, _ = make_bot(tmp_path, only=("trend_4h", "trend_1h"), manual=False, candles=c)
    for run in bot.runners.values():
        bot.scan(run)
    a, b = bot.runners["trend_4h"].broker, bot.runners["trend_1h"].broker
    assert len(a.open_positions()) == 1 and len(b.open_positions()) == 1
    assert a.state({})["cash"] == pytest.approx(b.state({})["cash"])
    assert (tmp_path / "trend_4h.sqlite").exists() and (tmp_path / "trend_1h.sqlite").exists()


def test_scalp_time_exit_then_cooldown(tmp_path):
    c = first_setup(noisy_uptrend(), S["scalp_15m"], start=60)
    bot, tg, mkt = make_bot(tmp_path, only=("scalp_15m",), manual=False, candles=c)
    run = bot.runners["scalp_15m"]
    bot.scan(run)
    pos = run.broker.open_positions()[0]
    run.broker.db.execute("UPDATE positions SET opened_ts = opened_ts - 1500")  # 25 min old
    run.broker.db.commit()
    bot.monitor()
    assert run.broker.open_positions() == []
    assert "(time)" in tg.sent[-1][0]

    run.last_candle = {}  # pretend a new bar with the same setup
    bot.scan(run)
    assert run.broker.open_positions() == []  # blocked by 90 min cooldown


def test_kill_switch_per_strategy(tmp_path):
    bot, tg, _ = make_bot(tmp_path, manual=False)
    run = bot.runners["trend_4h"]
    bot.scan(run)
    run.broker._set("peak", 1000)
    run.broker.db.commit()
    bot.monitor()
    assert "Kill switch" in tg.sent[-1][0] and run.broker.state({})["killed"]
    bot.command(["/resetkill", "trend_4h"])
    assert not run.broker.state({})["killed"]


def test_ranking_and_commands(tmp_path):
    bot, tg, _ = make_bot(tmp_path, only=("trend_4h", "bounce_1h"))
    b = bot.runners["bounce_1h"].broker
    b.sell(opened(b, qty=1.0)["id"], 110, "test")
    text = bot.ranking()
    assert text.index("bounce_1h") < text.index("trend_4h")
    bot.command(["/stats"])
    assert "ranking" in tg.sent[-1][0]
    bot.command(["/stats", "bounce_1h"])
    assert "BTC-USD: 1 trades" in tg.sent[-1][0]
    bot.command(["/status"])
    assert "trend_4h" in tg.sent[-1][0] and "bounce_1h" in tg.sent[-1][0]


def test_weekly_report_once_per_week(tmp_path):
    bot, tg, _ = make_bot(tmp_path)
    tz = ZoneInfo("Asia/Nicosia")
    assert not bot.weekly_report(datetime(2026, 11, 1, 10, 0, tzinfo=tz))
    assert not bot.weekly_report(datetime(2026, 11, 2, 18, 5, tzinfo=tz))
    assert bot.weekly_report(datetime(2026, 11, 1, 18, 5, tzinfo=tz))
    assert "Weekly report" in tg.sent[-1][0] and "ranking" in tg.sent[-1][0]
    assert not bot.weekly_report(datetime(2026, 11, 1, 18, 5, tzinfo=tz))
    assert bot.weekly_report(datetime(2026, 11, 8, 19, 0, tzinfo=tz))


def test_scan_schedule_aligns_to_candle_close():
    from bot.main import Runner
    r = Runner("x", {"interval_min": 15}, None)
    r.schedule_next(1_000_000)  # 1_000_000 s -> next 900 s boundary is 1_000_800
    assert r.next_scan == 1_000_815


# --- daily trend filter, liquidity, go-live ---------------------------------
def daily(closes, interval=1440, vol=1e9):
    start = int(time.time() * 1000) - (len(closes) + 2) * interval * 60_000
    return [{"start": start + i * interval * 60_000, "open": c, "high": c * 1.01,
             "low": c * 0.99, "close": c, "volume": vol / c} for i, c in enumerate(closes)]


def test_regime_entry_and_exit_signal():
    s = S["trend_daily"]
    up = daily([100 + i * 0.5 for i in range(220)])
    found = strategy.find_setup("BTC-USD", up, s, IND)
    assert found and found["stop"] == pytest.approx(found["entry"] * 0.8)
    assert not strategy.exit_signal(up, s)
    down = daily([100 + i * 0.5 for i in range(220)] + [100] * 5)
    assert strategy.find_setup("BTC-USD", down, s, IND) is None
    assert strategy.exit_signal(down, s)
    assert strategy.find_setup("BTC-USD", up[:150], s, IND) is None  # not enough history


def test_regime_sizing_ignores_rr_and_uses_allocation():
    d = risk.check(setup(entry=100, stop=80, target=10_000), base_state(), R, FEE,
                   position_pct=0.5, max_dd=0.35)
    assert d.ok and d.qty == pytest.approx(1.0)  # 50% of 200 / 100
    # 20% drawdown would kill a normal strategy but not the trend filter
    st = base_state(equity=160.0, day_start_equity=160.0)
    assert not risk.check(setup(), st, R, FEE).ok
    assert risk.check(setup(entry=100, stop=80, target=10_000), st, R, FEE,
                      position_pct=0.5, max_dd=0.35).ok


def test_regime_full_cycle(tmp_path):
    up = daily([100 + i * 0.5 for i in range(220)])
    bot, tg, mkt = make_bot(tmp_path, only=("trend_daily",), manual=False, candles=up)
    run = bot.runners["trend_daily"]
    run.s["pairs"] = ["BTC-USD"]  # fake market returns the same candles for every pair
    bot.scan(run)
    pos = run.broker.open_positions()
    assert len(pos) == 1 and pos[0]["qty"] * pos[0]["entry"] == pytest.approx(100, rel=0.01)
    assert "Holds until a daily close below" in tg.sent[-1][0]
    run.last_candle = {}
    bot.scan(run)  # still above average: no second buy, no exit
    assert len(run.broker.open_positions()) == 1
    mkt.c = daily([100 + i * 0.5 for i in range(220)] + [100] * 5)
    mkt.price = 100
    bot._candles.clear()  # next day: fresh candles
    bot.scan(run)
    assert run.broker.open_positions() == []
    assert "trend ended" in tg.sent[-1][0]


def test_liquidity_filter_skips_thin_pairs(tmp_path):
    thin = daily([100 + i * 0.5 for i in range(220)], vol=50_000)
    bot, tg, _ = make_bot(tmp_path, only=("trend_daily",), manual=False, candles=thin)
    bot.cfg["exchange"]["min_volume_usd_24h"] = 100_000
    bot.scan(bot.runners["trend_daily"])
    assert bot.runners["trend_daily"].broker.open_positions() == []
    assert strategy.quote_volume_24h(thin, 1440) == pytest.approx(50_000)


def test_max_drawdown_stat_and_go_live(tmp_path):
    bot, tg, _ = make_bot(tmp_path, only=("trend_4h", "trend_daily"))
    b = bot.runners["trend_4h"].broker
    b.sell(opened(b, qty=1.0)["id"], 120, "t")   # +20 -> 220
    b.sell(opened(b, qty=1.0)["id"], 80, "t")    # -20 -> 200, dd 9.1%
    s = b.stats()
    assert s["max_drawdown"] == pytest.approx(20 / 220, rel=0.02)  # small fee effect
    text = bot.go_live_text()
    assert "trend_4h: ❌ not yet" in text and "trades 2/30" in text
    assert "trend_daily: judged on multi-year history" in text


def test_ranking_counts_open_positions(tmp_path):
    bot, tg, _ = make_bot(tmp_path, only=("trend_4h", "bounce_1h"))
    b = bot.runners["bounce_1h"].broker
    opened(b, qty=1.0, price=100.0)
    bot.prices["BTC-USD"] = 130.0  # +30 unrealised
    text = bot.ranking()
    assert text.index("bounce_1h") < text.index("trend_4h") and "1 open" in text


def test_exit_sends_trade_chart(tmp_path):
    bot, tg, mkt = make_bot(tmp_path, manual=False)
    run = bot.runners["trend_4h"]
    bot.scan(run)
    pos = run.broker.open_positions()[0]
    mkt.price = mkt.hi = pos["target"] + 1
    mkt.lo = pos["entry"]
    bot.monitor()
    caption, kind = tg.sent[-1]
    assert kind == "photo" and "closed (target)" in caption


def test_chart_renders_png():
    from bot import charts
    c = make_candles(n=80)
    t0 = c[40]["start"] / 1000
    pos = {"opened_ts": t0, "closed_ts": t0 + 10 * 14400, "entry": c[40]["close"],
           "exit_price": c[50]["close"], "stop": c[40]["close"] * 0.95,
           "target": c[40]["close"] * 1.1}
    assert charts.trade_chart(c, pos, "test", 240)[:4] == b"\x89PNG"
