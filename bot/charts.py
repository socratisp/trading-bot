"""Per-trade chart: candles around the trade, buy and sell markers, stop and target lines."""
import io
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402


def _px(x):
    return f"{x:,.2f}" if x >= 100 else (f"{x:,.4f}" if x >= 1 else f"{x:,.6f}")


def _dt(ts_seconds):
    return datetime.fromtimestamp(ts_seconds, timezone.utc)


def trade_chart(candles, pos, title, interval_min, pad_bars=30):
    """candles: oldest first. pos: position row incl. opened_ts, closed_ts, entry,
    exit_price, stop, target. Returns PNG bytes."""
    opened, closed = pos["opened_ts"], pos.get("closed_ts")
    span = interval_min * 60
    lo = opened - pad_bars * span
    hi = (closed or opened) + 5 * span
    view = [c for c in candles if lo <= c["start"] / 1000 <= hi] or candles[-60:]

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=110)
    width = span / 86400 * 0.7  # matplotlib dates are in days
    for c in view:
        x = mdates.date2num(_dt(c["start"] / 1000 + span / 2))
        up = c["close"] >= c["open"]
        color = "#16a34a" if up else "#dc2626"
        ax.vlines(x, c["low"], c["high"], color=color, linewidth=0.8)
        ax.bar(x, abs(c["close"] - c["open"]) or c["close"] * 1e-4,
               bottom=min(c["open"], c["close"]), width=width, color=color, linewidth=0)

    ax.axhline(pos["stop"], color="#dc2626", linestyle="--", linewidth=1, label=f"stop {_px(pos['stop'])}")
    show_target = pos["target"] < pos["entry"] * 3  # trend filter has no real target
    if show_target:
        ax.axhline(pos["target"], color="#16a34a", linestyle="--", linewidth=1, label=f"target {_px(pos['target'])}")
    ax.scatter([_dt(opened)], [pos["entry"]], marker="^", s=140, color="#2563eb",
               zorder=5, label=f"buy {_px(pos['entry'])}")
    if closed and pos.get("exit_price"):
        win = pos["exit_price"] > pos["entry"]
        ax.scatter([_dt(closed)], [pos["exit_price"]], marker="v", s=140,
                   color="#16a34a" if win else "#dc2626", zorder=5,
                   label=f"sell {_px(pos['exit_price'])}")

    prices = [c["high"] for c in view] + [c["low"] for c in view] + [pos["stop"], pos["entry"]]
    if show_target:
        prices.append(pos["target"])
    pad = (max(prices) - min(prices)) * 0.05
    ax.set_ylim(min(prices) - pad, max(prices) + pad)
    ax.set_title(title, fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d %b %H:%M"))
    fig.autofmt_xdate()
    ax.grid(alpha=0.2)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
