"""Rough replay of every strategy over recent Revolut X history (up to 1000 candles).

Usage: python -m scripts.replay
Ignores position limits and the daily/kill-switch rules, so treat it as a sanity
check of the entry/exit rules, not a precise backtest. Results are in R:
1R = the planned risk of one trade (1% of equity), so -10R ~ -10% of the account.
"""
import yaml

from bot import risk, strategy
from bot.market import Market


def replay_regime(name, s, m, fee):
    """Trend filter: % return and worst drop vs simply holding the coin."""
    for pair in s["pairs"]:
        c = m.candles(pair, s["interval_min"])
        cl = [x["close"] for x in c]
        n = s["sma"]
        if len(c) <= n + 10:
            continue
        eq, pos, peak, mdd, trades = 1.0, None, 1.0, 0.0, 0
        for i in range(n, len(c)):
            avg = sum(cl[i - n + 1:i + 1]) / n
            if pos is None and cl[i] > avg:
                pos, trades = cl[i], trades + 1
            elif pos is not None:
                stop = pos * (1 - s["stop_pct"])
                exit_price = stop if c[i]["low"] <= stop else (cl[i] if cl[i] < avg else None)
                if exit_price:
                    eq *= exit_price * (1 - fee) / (pos * (1 + fee))
                    pos = None
            cur = eq * (cl[i] / pos if pos else 1)
            peak, mdd = max(peak, cur), max(mdd, 1 - cur / max(peak, cur))
        final = eq * (cl[-1] / pos if pos else 1)
        hold = cl[-1] / cl[n]
        hold_peak, hold_mdd = 0, 0
        for x in cl[n:]:
            hold_peak = max(hold_peak, x)
            hold_mdd = max(hold_mdd, 1 - x / hold_peak)
        print(f"{name:10s} {pair} {len(c) - n} days | {trades} trades | {final - 1:+.0%}, "
              f"worst drop {mdd:.0%} | just holding: {hold - 1:+.0%}, worst drop {hold_mdd:.0%}")


def replay(cfg):
    m = Market(cfg["exchange"]["base_url"], cfg["exchange"].get("region", "EEA"))
    fee, ind = cfg["exchange"]["fee_rate"], cfg["indicators"]
    cache = {}
    for name, s in cfg["strategies"].items():
        if s["type"] == "regime":
            replay_regime(name, s, m, fee)
            continue
        iv = s["interval_min"]
        trades, span_days = [], 0.0
        for pair in cfg["exchange"]["pairs"]:
            if (pair, iv) not in cache:
                try:
                    cache[(pair, iv)] = m.candles(pair, iv)
                except Exception:
                    cache[(pair, iv)] = []
            c = cache[(pair, iv)]
            if len(c) < 260:
                continue
            span_days = max(span_days, (c[-1]["start"] - c[210]["start"]) / 86_400_000)
            busy_until, last_exit = 0, 0
            for e in range(210, len(c) - 1):
                if e < busy_until:
                    continue
                st = strategy.find_setup(pair, c[:e + 1], s, ind)
                if not st:
                    continue
                if s.get("cooldown_min") and c[e]["start"] - last_exit < s["cooldown_min"] * 60_000:
                    continue
                rr = risk.reward_risk_after_fees(st["entry"], st["stop"], st["target"], fee)
                if rr < s.get("min_reward_risk", cfg["risk"]["min_reward_risk"]):
                    continue
                entry = c[e + 1]["open"]
                max_bars = max(1, s["max_hold_min"] // iv) if s.get("max_hold_min") else 10 ** 9
                result = None
                stop = st["stop"]
                be_at = s.get("breakeven_at")
                for k in range(e + 1, len(c)):
                    b = c[k]
                    if b["low"] <= stop:
                        exit_price = stop
                    elif b["high"] >= st["target"]:
                        exit_price = st["target"]
                    elif k - e >= max_bars:
                        exit_price = b["close"]
                    else:
                        if be_at and stop < entry and b["high"] >= entry + (st["target"] - entry) * be_at:
                            stop = entry * (1 + fee) / (1 - fee)
                        continue
                    pnl = (exit_price * (1 - fee) - entry * (1 + fee)) / entry
                    result = pnl / ((entry - st["stop"]) / entry)
                    break
                if result is None:
                    continue
                trades.append(result)
                busy_until, last_exit = k, c[k]["start"]
        n = len(trades)
        wins = sum(t > 0 for t in trades)
        print(f"{name:10s} {span_days:5.1f} days | {n:3d} trades ({n / max(span_days, 1):.2f}/day) | "
              f"win {wins / max(n, 1):.0%} | avg {sum(trades) / max(n, 1):+.2f}R | "
              f"total {sum(trades):+.1f}R")


if __name__ == "__main__":
    replay(yaml.safe_load(open("config.yaml")))
