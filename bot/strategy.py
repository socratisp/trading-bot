"""Deterministic setup finder. The LLM never sets entry, stop, target or size.

Rule (long only, spot): trend is up (EMA fast > EMA slow and close > EMA fast)
and price has pulled back (RSI in the pullback zone). Stop and target are
ATR multiples from the last close.
"""
from .indicators import atr, ema, rsi


def find_setup(pair, candles, s):
    need = max(s["ema_slow"], s["atr_period"], s["rsi_period"]) + 5
    if len(candles) < need:
        return None
    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]

    fast = ema(closes, s["ema_fast"])[-1]
    slow = ema(closes, s["ema_slow"])[-1]
    r = rsi(closes, s["rsi_period"])
    a = atr(highs, lows, closes, s["atr_period"])
    close = closes[-1]

    snapshot = {"close": close, "ema_fast": fast, "ema_slow": slow,
                "rsi": r, "atr": a, "candle_start": candles[-1]["start"]}

    uptrend = fast > slow and close > fast
    pullback = s["rsi_min"] <= r <= s["rsi_max"]
    if not (uptrend and pullback and a and a > 0):
        return None

    return {
        "pair": pair,
        "side": "buy",
        "entry": close,
        "stop": close - s["stop_atr_mult"] * a,
        "target": close + s["target_atr_mult"] * a,
        "setup": (f"Uptrend pullback: EMA{s['ema_fast']} > EMA{s['ema_slow']}, "
                  f"close above EMA{s['ema_fast']}, RSI {r:.1f}"),
        "snapshot": snapshot,
    }
