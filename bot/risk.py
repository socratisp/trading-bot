"""Risk engine. Every proposal and every fill passes through here."""
from dataclasses import dataclass


@dataclass
class Decision:
    ok: bool
    reason: str
    qty: float = 0.0
    risk_usd: float = 0.0
    reward_risk: float = 0.0


def reward_risk_after_fees(entry, stop, target, fee_rate):
    """R:R per unit, with buy fee on entry and sell fee on exit."""
    risk = (entry - stop) + entry * fee_rate + stop * fee_rate
    reward = (target - entry) - entry * fee_rate - target * fee_rate
    if risk <= 0:
        return 0.0
    return reward / risk


def size(entry, stop, equity, r):
    """Units to buy so a stop-out loses risk_per_trade of equity."""
    loss_per_unit = entry - stop
    if loss_per_unit <= 0:
        return 0.0
    qty = (equity * r["risk_per_trade"]) / loss_per_unit
    cap = (equity * r["max_position_pct"]) / entry
    return min(qty, cap)


def check(setup, state, r, fee, min_rr=None, position_pct=None, max_dd=None):
    """r: shared risk rules. state: equity, cash, peak, day_start_equity, killed, open_pairs.
    min_rr: per-strategy override of r['min_reward_risk'].
    position_pct: allocation sizing (trend filter) instead of 1%-risk sizing; skips the R:R rule.
    max_dd: per-strategy override of r['max_drawdown_kill']."""
    min_rr = r["min_reward_risk"] if min_rr is None else min_rr
    max_dd = r["max_drawdown_kill"] if max_dd is None else max_dd

    if state["killed"]:
        return Decision(False, "kill switch active (max drawdown hit) - send /resetkill after review")
    if state["peak"] > 0 and state["equity"] <= state["peak"] * (1 - max_dd):
        return Decision(False, "max drawdown reached - kill switch")
    if state["equity"] <= state["day_start_equity"] * (1 - r["daily_loss_halt"]):
        return Decision(False, "daily loss limit hit - paused until tomorrow (UTC)")
    if len(state["open_pairs"]) >= r["max_open_positions"]:
        return Decision(False, "max open positions reached")
    if setup["pair"] in state["open_pairs"]:
        return Decision(False, "already holding this pair")
    if not (setup["stop"] < setup["entry"] < setup["target"]):
        return Decision(False, "invalid levels")

    if position_pct:
        rr = 0.0
        qty = state["equity"] * position_pct / setup["entry"]
    else:
        rr = reward_risk_after_fees(setup["entry"], setup["stop"], setup["target"], fee)
        if rr < min_rr:
            return Decision(False, f"reward:risk {rr:.2f} after fees is below {min_rr}")
        qty = size(setup["entry"], setup["stop"], state["equity"], r)
    if qty * setup["entry"] * (1 + fee) > state["cash"]:
        qty = state["cash"] / (setup["entry"] * (1 + fee))
    if qty * setup["entry"] < 5:  # dust guard
        return Decision(False, "position too small")

    risk_usd = qty * (setup["entry"] - setup["stop"]) + qty * (setup["entry"] + setup["stop"]) * fee
    return Decision(True, "passed", qty=qty, risk_usd=risk_usd, reward_risk=rr)
