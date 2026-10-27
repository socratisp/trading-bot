"""Deterministic setup finders. The LLM never sets entry, stop, target or size.

trend:  uptrend (EMA fast > EMA slow, close > EMA fast) and a dip (RSI in range).
bounce: oversold (RSI below rsi_max) and the last candle closed higher than the
        one before it (first sign of a turn). No trend filter, so it trades in
        downtrends - that is the point, and the risk.
Stop and target are ATR multiples from the last close. Long only (spot).
"""
from .indicators import atr, ema, rsi


def find_setup(pair, candles, s, ind):
    slow = s.get("ema_slow", 0)
    need = max(slow, ind["atr_period"], ind["rsi_period"]) + 5
    if len(candles) < need:
        return None
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    r = rsi(closes, ind["rsi_period"])
    a = atr(highs, lows, closes, ind["atr_period"])
    close = closes[-1]
    if not a or a <= 0 or r is None:
        return None
    snapshot = {"close": close, "rsi": r, "atr": a, "candle_start": candles[-1]["start"]}

    if s["type"] == "trend":
        fast_v = ema(closes, s["ema_fast"])[-1]
        slow_v = ema(closes, s["ema_slow"])[-1]
        snapshot.update(ema_fast=fast_v, ema_slow=slow_v)
        if not (fast_v > slow_v and close > fast_v and s["rsi_min"] <= r <= s["rsi_max"]):
            return None
        why = (f"Uptrend dip: EMA{s['ema_fast']} > EMA{s['ema_slow']}, "
               f"close above EMA{s['ema_fast']}, RSI {r:.1f}")
    elif s["type"] == "bounce":
        if not (r < s["rsi_max"] and closes[-1] > closes[-2]):
            return None
        why = f"Oversold bounce: RSI {r:.1f}, last candle closed higher"
    else:
        raise ValueError(f"unknown strategy type {s['type']}")

    return {
        "pair": pair, "side": "buy", "entry": close,
        "stop": close - s["stop_atr_mult"] * a,
        "target": close + s["target_atr_mult"] * a,
        "setup": why, "snapshot": snapshot,
    }
