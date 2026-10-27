"""Main loop. Several strategies run side by side, each with its own paper account:
candle close -> setup -> risk check -> (optional Claude veto) -> paper fill.
Exits (stop, target, max hold time) are watched every monitor interval."""
import logging
import math
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import yaml

from . import llm, news, risk, strategy
from .market import Market
from .paper import PaperBroker
from .telegram import Telegram

log = logging.getLogger("bot")


def load_config(path):
    with open(path) as f:
        cfg = yaml.safe_load(f)
    if cfg.get("mode") != "paper":
        sys.exit("Only paper mode exists in this version. Set mode: paper.")
    return cfg


def fmt(x):
    return f"{x:,.6f}" if x < 1 else (f"{x:,.4f}" if x < 100 else f"{x:,.2f}")


class Runner:
    """One strategy with its own paper account."""

    def __init__(self, name, s, broker):
        self.name, self.s, self.broker = name, s, broker
        self.last_candle = {}
        self.next_scan = 0.0

    def schedule_next(self, now):
        """Scan 15 s after the next candle close."""
        span = self.s["interval_min"] * 60
        self.next_scan = math.floor(now / span) * span + span + 15


class Bot:
    def __init__(self, cfg, market=None, tg=None, reviewer=None, headlines=None):
        self.cfg = cfg
        ex = cfg["exchange"]
        self.fee = ex["fee_rate"]
        self.market = market or Market(ex["base_url"], ex.get("region", "EEA"))
        self.tg = tg or Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.runners = {
            name: Runner(name, s, PaperBroker(os.path.join(cfg["data_dir"], f"{name}.sqlite"),
                                              cfg["account"]["starting_equity"], self.fee))
            for name, s in cfg["strategies"].items()
        }
        self.meta = next(iter(self.runners.values())).broker  # holds shared flags
        self.reviewer = reviewer or llm.review
        self.headlines = headlines or (lambda: news.headlines(cfg["news"]["feeds"],
                                                              cfg["news"]["max_items"]))
        self.prices = {}
        self._candles = {}
        self.scans = 0
        self.last_scan_ts = None

    def send(self, run, text, buttons=None):
        self.tg.send(f"[{run.name}] {text}", buttons)

    # --- data ----------------------------------------------------------------
    def candles(self, pair, interval):
        """Cached per pair+interval for 60 s, so trend_1h and bounce_1h share one request."""
        key, now = (pair, interval), time.time()
        hit = self._candles.get(key)
        if hit and now - hit[0] < 60:
            return hit[1]
        data = self.market.candles(pair, interval)
        self._candles[key] = (now, data)
        return data

    # --- monitoring ----------------------------------------------------------
    def monitor(self):
        pairs = {p["pair"] for r in self.runners.values() for p in r.broker.open_positions()}
        quotes = {}
        for pair in pairs:
            try:
                quotes[pair] = self.market.last_price(pair)
                self.prices[pair] = quotes[pair][0]
            except Exception as exc:
                log.warning("price failed %s: %s", pair, exc)
        dd = self.cfg["risk"]["max_drawdown_kill"]
        for run in self.runners.values():
            for pair, (last, high, low) in quotes.items():
                for pos, why, pnl in run.broker.check_exits(pair, last, high, low,
                                                            run.s.get("max_hold_min")):
                    icon = "✅" if pnl > 0 else "🛑"
                    self.send(run, f"{icon} <b>{pair}</b> closed ({why}): P&L {pnl:+.2f} USD")
            st = run.broker.state(self.prices)
            if not st["killed"] and st["equity"] <= st["peak"] * (1 - dd):
                run.broker.kill()
                self.send(run, f"⛔ Kill switch: equity {st['equity']:.2f} is {dd:.0%} below peak. "
                               f"Paused. Send /resetkill {run.name} after you review.")

    # --- scanning ------------------------------------------------------------
    def scan(self, run):
        s = run.s
        heads = None
        cooldown = s.get("cooldown_min", 0) * 60
        for pair in self.cfg["exchange"]["pairs"]:
            try:
                candles = self.candles(pair, s["interval_min"])
            except Exception as exc:
                log.warning("candles failed %s: %s", pair, exc)
                continue
            if not candles:
                continue
            self.prices.setdefault(pair, candles[-1]["close"])
            if run.last_candle.get(pair) == candles[-1]["start"]:
                continue  # already evaluated this bar
            run.last_candle[pair] = candles[-1]["start"]

            setup = strategy.find_setup(pair, candles, s, self.cfg["indicators"])
            if not setup:
                continue
            if cooldown and time.time() - run.broker.last_exit_ts(pair) < cooldown:
                log.info("[%s] %s setup skipped: cooldown", run.name, pair)
                continue
            d = risk.check(setup, run.broker.state(self.prices), self.cfg["risk"], self.fee,
                           s.get("min_reward_risk"))
            if not d.ok:
                run.broker.log_proposal(setup, d, None, d.reason, "rejected_risk")
                log.info("[%s] %s setup rejected: %s", run.name, pair, d.reason)
                continue

            verdict, reason = "skipped", "no news check for this strategy"
            if s.get("llm_review") and self.cfg["llm"]["enabled"]:
                if heads is None:
                    heads = self.headlines()
                verdict, reason = self.reviewer(setup, heads, self.cfg["llm"]["model"],
                                                self.cfg["llm"]["timeout_sec"])
            if verdict == "veto":
                run.broker.log_proposal(setup, d, verdict, reason, "vetoed")
                self.send(run, f"🚫 {pair} setup vetoed by Claude: {reason}")
                continue

            if self.cfg["approval"]["required"]:
                pid = run.broker.log_proposal(setup, d, verdict, reason, "pending")
                self.propose(run, pid, setup, d, verdict, reason)
            else:
                pid = run.broker.log_proposal(setup, d, verdict, reason, "approved")
                self.fill(run, pid)

    def propose(self, run, pid, setup, d, verdict, reason):
        mins = self.cfg["approval"]["expiry_min"]
        text = (f"📈 <b>Paper trade #{pid}: BUY {setup['pair']}</b>\n"
                f"Entry {fmt(setup['entry'])} | Stop {fmt(setup['stop'])} | "
                f"Target {fmt(setup['target'])}\n"
                f"Size ~{d.qty * setup['entry']:.2f} USD | Risk {d.risk_usd:.2f} USD | "
                f"R:R {d.reward_risk:.2f} after fees\n"
                f"Why: {setup['setup']}\nClaude: {verdict} - {reason}\nExpires in {mins} min.")
        self.send(run, text, buttons=[
            {"text": "✅ Approve", "callback_data": f"ok:{run.name}:{pid}"},
            {"text": "❌ Reject", "callback_data": f"no:{run.name}:{pid}"}])

    def fill(self, run, pid):
        p = run.broker.proposal(pid)
        try:
            price, _, _ = self.market.last_price(p["pair"])
        except Exception as exc:
            self.send(run, f"Could not get price for {p['pair']}: {exc}")
            return
        self.prices[p["pair"]] = price
        setup = {"pair": p["pair"], "entry": price, "stop": p["stop"], "target": p["target"]}
        d = risk.check(setup, run.broker.state(self.prices), self.cfg["risk"], self.fee,
                       run.s.get("min_reward_risk"))
        if not d.ok:  # market moved since the candle closed
            run.broker.set_status(pid, "rejected_on_fill")
            self.send(run, f"#{pid} {p['pair']} not filled: {d.reason}")
            return
        qty = run.broker.buy(pid, price)
        run.broker.set_status(pid, "filled")
        hold = run.s.get("max_hold_min")
        self.send(run, f"🟢 #{pid} paper BUY {p['pair']} ~{qty * price:.2f} USD at {fmt(price)}\n"
                       f"Stop {fmt(p['stop'])} | Target {fmt(p['target'])} | R:R {d.reward_risk:.2f}"
                       + (f" | max {hold} min" if hold else "") +
                       f"\nWhy: {p['setup']}\nClaude: {p['llm_verdict']} - {p['llm_reason']}")

    # --- telegram ------------------------------------------------------------
    def handle_telegram(self):
        for kind, data in self.tg.updates():
            if kind == "button":
                parts = data.split(":")
                if len(parts) != 3 or parts[1] not in self.runners or not parts[2].isdigit():
                    continue
                action, run, pid = parts[0], self.runners[parts[1]], int(parts[2])
                p = run.broker.proposal(pid)
                if not p or p["status"] != "pending":
                    self.send(run, f"#{pid} is no longer pending.")
                elif action == "ok":
                    run.broker.set_status(pid, "approved")
                    self.fill(run, pid)
                else:
                    run.broker.set_status(pid, "rejected_by_user")
                    self.send(run, f"#{pid} rejected.")
            elif kind == "command":
                self.command(data.split())

    def command(self, words):
        cmd, arg = words[0].lower(), (words[1].lower() if len(words) > 1 else None)
        if cmd == "/status":
            lines = []
            for run in self.runners.values():
                st = run.broker.state(self.prices)
                lines.append(f"<b>{run.name}</b>: equity {st['equity']:.2f}"
                             + (" ⛔ KILLED" if st["killed"] else ""))
                for pos in run.broker.open_positions():
                    lines.append(f"  {pos['pair']} @ {fmt(pos['entry'])} stop {fmt(pos['stop'])} "
                                 f"target {fmt(pos['target'])}")
            self.tg.send("\n".join(lines))
        elif cmd == "/stats":
            if arg in self.runners:
                self.tg.send(f"<b>{arg}</b>\n" + self.stats_text(self.runners[arg], detail=True))
            else:
                self.tg.send(self.ranking() + "\n\nDetails: /stats " + " | /stats ".join(self.runners))
        elif cmd == "/resetkill":
            targets = list(self.runners.values()) if arg in (None, "all") else (
                [self.runners[arg]] if arg in self.runners else [])
            for run in targets:
                run.broker.reset_kill()
            self.tg.send(f"Kill switch reset: {', '.join(r.name for r in targets) or 'nothing'}")
        else:
            self.tg.send("Commands: /status  /stats  /stats &lt;strategy&gt;  /resetkill [strategy|all]")

    def stats_text(self, run, detail=False):
        s = run.broker.stats()
        text = (f"Closed {s['closed_trades']} | Win {s['win_rate']:.0%} | "
                f"Net {s['net_pnl']:+.2f} | Exp {s['expectancy']:+.2f}/trade\n"
                f"Avg win {s['avg_win']:+.2f} | Avg loss {s['avg_loss']:+.2f}")
        if detail:
            text += f"\nSetups: {s['proposals']}" + "".join(
                f"\n{p['pair']}: {p['trades']} trades, {p['wins']} wins, {p['pnl']:+.2f}"
                for p in s["by_pair"])
        return text

    def ranking(self):
        rows = []
        for run in self.runners.values():
            s = run.broker.stats()
            rows.append((s["net_pnl"], run.name, s))
        rows.sort(key=lambda r: r[0], reverse=True)
        lines = ["🏁 <b>Strategy ranking</b> (net P&L after fees)"]
        for i, (pnl, name, s) in enumerate(rows, 1):
            lines.append(f"{i}. {name}: {pnl:+.2f} | {s['closed_trades']} trades | "
                         f"win {s['win_rate']:.0%} | exp {s['expectancy']:+.2f}")
        return "\n".join(lines)

    # --- weekly report ---------------------------------------------------------
    def report_due(self, now=None):
        r = self.cfg.get("report")
        if not r:
            return None
        local = now or datetime.now(ZoneInfo(r["timezone"]))
        key = local.strftime("%G-W%V")
        if (local.weekday() == r["weekday"] and local.hour >= r["hour"]
                and self.meta._get("last_report") != key):
            return key
        return None

    def weekly_report(self, now=None):
        key = self.report_due(now)
        if not key:
            return False
        tz = ZoneInfo(self.cfg["report"]["timezone"])
        last = (datetime.fromtimestamp(self.last_scan_ts, tz).strftime("%a %H:%M")
                if self.last_scan_ts else "never")
        open_n = sum(len(r.broker.open_positions()) for r in self.runners.values())
        self.tg.send(f"📊 <b>Weekly report</b> - bot alive ✅\n"
                     f"Scans this week: {self.scans} | last scan {last} | open positions {open_n}\n"
                     f"Each strategy started with {self.cfg['account']['starting_equity']} USD.\n\n"
                     + self.ranking())
        self.meta._set("last_report", key)
        self.meta.db.commit()
        self.scans = 0
        return True

    def expire(self):
        limit = self.cfg["approval"]["expiry_min"] * 60
        for run in self.runners.values():
            for p in run.broker.pending():
                if time.time() - p["ts"] > limit:
                    run.broker.set_status(p["id"], "expired")
                    self.send(run, f"⌛ #{p['id']} expired.")

    # --- loop ------------------------------------------------------------------
    def run(self):
        names = ", ".join(self.runners)
        self.tg.send(f"🤖 Bot started in PAPER mode. Strategies: {names}\n/status /stats")
        next_monitor = 0.0
        while True:
            now = time.time()
            try:
                self.handle_telegram()
                self.expire()
                self.weekly_report()
                if now >= next_monitor:
                    self.monitor()
                    next_monitor = now + self.cfg["schedule"]["monitor_every_sec"]
                for run in self.runners.values():
                    if now >= run.next_scan:
                        self.scan(run)
                        log.info("[%s] scan done", run.name)
                        self.scans += 1
                        self.last_scan_ts = time.time()
                        run.schedule_next(time.time())
            except Exception:
                log.exception("loop error")
            time.sleep(5)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(os.getenv("BOT_CONFIG", "config.yaml"))
    Bot(cfg).run()


if __name__ == "__main__":
    main()
