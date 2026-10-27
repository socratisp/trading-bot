"""Main loop: scan -> risk check -> Claude veto -> Telegram approval -> paper fill.
Exits (stop/target) are watched every monitor interval."""
import logging
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
    return f"{x:,.4f}" if x < 100 else f"{x:,.2f}"


class Bot:
    def __init__(self, cfg, market=None, tg=None, broker=None, reviewer=None, headlines=None):
        self.cfg = cfg
        ex = cfg["exchange"]
        self.market = market or Market(ex["base_url"], ex.get("region", "EEA"))
        self.tg = tg or Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
        os.makedirs(cfg["data_dir"], exist_ok=True)
        self.broker = broker or PaperBroker(os.path.join(cfg["data_dir"], "bot.sqlite"),
                                            cfg["account"]["starting_equity"], ex["fee_rate"])
        self.reviewer = reviewer or llm.review
        self.headlines = headlines or (lambda: news.headlines(cfg["news"]["feeds"],
                                                              cfg["news"]["max_items"]))
        self.prices = {}
        self.last_candle = {}
        self.scans = 0
        self.last_scan_ts = None

    # --- monitoring ------------------------------------------------------
    def monitor(self):
        pairs = {p["pair"] for p in self.broker.open_positions()}
        for pair in pairs:
            try:
                last, high, low = self.market.last_price(pair)
            except Exception as exc:
                log.warning("price failed %s: %s", pair, exc)
                continue
            self.prices[pair] = last
            for pos, why, pnl in self.broker.check_exits(pair, last, high, low):
                self.tg.send(f"{'✅' if pnl > 0 else '🛑'} <b>{pair}</b> closed at {why}: "
                             f"P&L {pnl:+.2f} USD")
        st = self.broker.state(self.prices)
        dd = self.cfg["risk"]["max_drawdown_kill"]
        if not st["killed"] and st["equity"] <= st["peak"] * (1 - dd):
            self.broker.kill()
            self.tg.send(f"⛔ Kill switch: equity {st['equity']:.2f} is {dd:.0%} below peak. "
                         "No new trades. Send /resetkill after you review.")

    # --- scanning --------------------------------------------------------
    def scan(self):
        ex, s = self.cfg["exchange"], self.cfg["strategy"]
        heads = None
        for pair in ex["pairs"]:
            try:
                candles = self.market.candles(pair, ex["interval_min"])
            except Exception as exc:
                log.warning("candles failed %s: %s", pair, exc)
                continue
            if not candles:
                continue
            self.prices.setdefault(pair, candles[-1]["close"])
            if self.last_candle.get(pair) == candles[-1]["start"]:
                continue  # already evaluated this bar
            self.last_candle[pair] = candles[-1]["start"]

            setup = strategy.find_setup(pair, candles, s)
            log.info("scan %s: close %.2f, %s", pair, candles[-1]["close"],
                     "SETUP FOUND" if setup else "no setup")
            if not setup:
                continue
            decision = risk.check(setup, self.broker.state(self.prices), self.cfg)
            if not decision.ok:
                self.broker.log_proposal(setup, decision, None, decision.reason, "rejected_risk")
                log.info("%s setup rejected by risk: %s", pair, decision.reason)
                continue

            verdict, reason = "skipped", "LLM disabled"
            if self.cfg["llm"]["enabled"]:
                if heads is None:
                    heads = self.headlines()
                verdict, reason = self.reviewer(setup, heads, self.cfg["llm"]["model"],
                                                self.cfg["llm"]["timeout_sec"])
            if verdict == "veto":
                self.broker.log_proposal(setup, decision, verdict, reason, "vetoed")
                self.tg.send(f"🚫 {pair} setup vetoed by Claude: {reason}")
                continue

            if self.cfg["approval"]["required"]:
                pid = self.broker.log_proposal(setup, decision, verdict, reason, "pending")
                self.propose(pid, setup, decision, verdict, reason)
            else:
                pid = self.broker.log_proposal(setup, decision, verdict, reason, "approved")
                self.fill(pid)

    def propose(self, pid, setup, d, verdict, reason):
        mins = self.cfg["approval"]["expiry_min"]
        text = (f"📈 <b>Paper trade #{pid}: BUY {setup['pair']}</b>\n"
                f"Entry {fmt(setup['entry'])} | Stop {fmt(setup['stop'])} | "
                f"Target {fmt(setup['target'])}\n"
                f"Size {d.qty:.6f} (~{d.qty * setup['entry']:.2f} USD)\n"
                f"Risk {d.risk_usd:.2f} USD | R:R {d.reward_risk:.2f} after fees\n"
                f"Why: {setup['setup']}\n"
                f"Claude: {verdict} - {reason}\n"
                f"Expires in {mins} min.")
        self.tg.send(text, buttons=[{"text": "✅ Approve", "callback_data": f"ok:{pid}"},
                                    {"text": "❌ Reject", "callback_data": f"no:{pid}"}])

    def fill(self, pid):
        p = self.broker.proposal(pid)
        try:
            price, _, _ = self.market.last_price(p["pair"])
        except Exception as exc:
            self.tg.send(f"Could not get price for {p['pair']}: {exc}")
            return
        self.prices[p["pair"]] = price
        # Re-check with the current price: the market may have moved.
        setup = {"pair": p["pair"], "entry": price, "stop": p["stop"], "target": p["target"]}
        d = risk.check(setup, self.broker.state(self.prices), self.cfg)
        if not d.ok:
            self.broker.set_status(pid, "rejected_on_fill")
            self.tg.send(f"#{pid} not filled: {d.reason}")
            return
        qty = self.broker.buy(pid, price)
        self.broker.set_status(pid, "filled")
        self.tg.send(f"🟢 #{pid} paper BUY {qty:.6f} {p['pair']} at {fmt(price)}\n"
                     f"Stop {fmt(p['stop'])} | Target {fmt(p['target'])} | "
                     f"R:R {d.reward_risk:.2f}\nWhy: {p['setup']}\n"
                     f"Claude: {p['llm_verdict']} - {p['llm_reason']}")

    # --- telegram ------------------------------------------------------------
    def handle_telegram(self):
        for kind, data in self.tg.updates():
            if kind == "button":
                action, _, pid = data.partition(":")
                p = self.broker.proposal(int(pid)) if pid.isdigit() else None
                if not p or p["status"] != "pending":
                    self.tg.send(f"#{pid} is no longer pending.")
                elif action == "ok":
                    self.broker.set_status(p["id"], "approved")
                    self.fill(p["id"])
                else:
                    self.broker.set_status(p["id"], "rejected_by_user")
                    self.tg.send(f"#{pid} rejected.")
            elif kind == "command":
                self.command(data.split()[0].lower())

    def command(self, cmd):
        if cmd == "/status":
            st = self.broker.state(self.prices)
            lines = [f"Equity {st['equity']:.2f} | Cash {st['cash']:.2f} | Peak {st['peak']:.2f}",
                     f"Kill switch: {'ON' if st['killed'] else 'off'}"]
            for pos in self.broker.open_positions():
                lines.append(f"{pos['pair']} {pos['qty']:.6f} @ {fmt(pos['entry'])} "
                             f"stop {fmt(pos['stop'])} target {fmt(pos['target'])}")
            self.tg.send("\n".join(lines))
        elif cmd == "/stats":
            self.tg.send(self.stats_text())
        elif cmd == "/resetkill":
            self.broker.reset_kill()
            self.tg.send("Kill switch reset. Trading resumes on the next scan.")
        else:
            self.tg.send("Commands: /status /stats /resetkill")

    def stats_text(self):
        s = self.broker.stats()
        return (f"Closed trades {s['closed_trades']} | Win rate {s['win_rate']:.0%}\n"
                f"Net P&L {s['net_pnl']:+.2f} | Expectancy {s['expectancy']:+.2f}/trade\n"
                f"Avg win {s['avg_win']:+.2f} | Avg loss {s['avg_loss']:+.2f}\n"
                f"Proposals: {s['proposals']}"
                + "".join(f"\n{p['pair']}: {p['trades']} trades, {p['wins']} wins, "
                          f"{p['pnl']:+.2f}" for p in s["by_pair"]))

    # --- weekly report ---------------------------------------------------
    def report_due(self, now=None):
        r = self.cfg.get("report")
        if not r:
            return None
        local = now or datetime.now(ZoneInfo(r["timezone"]))
        key = local.strftime("%G-W%V")
        if (local.weekday() == r["weekday"] and local.hour >= r["hour"]
                and self.broker._get("last_report") != key):
            return key
        return None

    def weekly_report(self, now=None):
        key = self.report_due(now)
        if not key:
            return False
        st = self.broker.state(self.prices)
        since = int(time.time()) - 7 * 86400
        setups = self.broker.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE ts >= ?", (since,)).fetchone()[0]
        last = (datetime.fromtimestamp(self.last_scan_ts, ZoneInfo(self.cfg["report"]["timezone"]))
                .strftime("%a %H:%M") if self.last_scan_ts else "never")
        self.tg.send(f"📊 <b>Weekly report</b> - bot alive ✅\n"
                     f"Scans this week: {self.scans} | last scan {last}\n"
                     f"Setups this week: {setups} | open positions: {len(st['open_pairs'])}\n"
                     f"Equity {st['equity']:.2f} (start {self.cfg['account']['starting_equity']}) | "
                     f"kill switch {'ON' if st['killed'] else 'off'}\n\n"
                     + self.stats_text())
        self.broker._set("last_report", key)
        self.broker.db.commit()
        self.scans = 0
        return True

    def expire(self):
        limit = self.cfg["approval"]["expiry_min"] * 60
        for p in self.broker.pending():
            if time.time() - p["ts"] > limit:
                self.broker.set_status(p["id"], "expired")
                self.tg.send(f"⌛ #{p['id']} expired.")

    # --- loop --------------------------------------------------------------
    def run(self):
        sched = self.cfg["schedule"]
        self.tg.send("🤖 Bot started in PAPER mode. /status /stats")
        next_scan = next_monitor = 0.0
        while True:
            now = time.time()
            try:
                self.handle_telegram()
                self.expire()
                self.weekly_report()
                if now >= next_monitor:
                    self.monitor()
                    next_monitor = now + sched["monitor_every_sec"]
                if now >= next_scan:
                    self.scan()
                    self.scans += 1
                    self.last_scan_ts = time.time()
                    next_scan = now + sched["scan_every_min"] * 60
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
