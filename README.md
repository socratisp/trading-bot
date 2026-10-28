# trading-bot (paper mode)

Rules-based crypto bot for Revolut X. Code finds setups and sets entry, stop,
target and size. Claude can only veto on news. You approve every trade in Telegram.
**Paper trading only. There is no live order code in this version.**

## Five competing strategies (each has its own $200 paper account)
| name | candles | buys when | exits |
|---|---|---|---|
| trend_daily | 1 day | BTC/ETH daily close above 200-day average (50% of account each) | daily close below the average, or -20% disaster stop |
| trend_4h | 4h | uptrend (EMA50 > EMA200) + dip (RSI 35-50) | stop 1.5 ATR / target 3.5 ATR |
| trend_1h | 1h | same rules on hourly candles | same, min R:R 1.5 |
| bounce_1h | 1h | oversold (RSI < 30) + first green candle | stop 1.5 / target 3.0 ATR, min R:R 1.3 |
| scalp_15m | 15m | EMA20 > EMA50 + dip | stop 1.5 / target 3.0 ATR, max 20 min hold, 90 min cooldown per coin |

Coins with under $100k traded in the last 24h are skipped automatically.
Shared rules: 1% risk per trade, max 2 positions per strategy, max 30% per coin,
-3% daily pause, -10% drawdown kill switch. Fees: 0.09% taker on both sides.
Scans run 15 s after each candle close. Claude news-veto only for the trend strategies.

## Going live (not built yet)
The Sunday report shows a go-live check per strategy: 30+ closed trades, positive
expectancy after fees, max drawdown under 15%. trend_daily is judged on the
multi-year replay instead, since it only trades a few times a year.

## Telegram
`/status` all strategies, `/stats` ranking, `/stats trend_4h` details,
`/resetkill trend_4h` or `/resetkill all`.
Every closed trade arrives as a chart (buy, sell, stop, target).
`/tradecharts` last 10 trades, `/tradecharts 7d`, `/tradecharts 2026-10-01 2026-10-28`,
`/tradecharts trend_1h 7d` - max 20 charts per request. Weekly ranking every Sunday 18:00 (Cyprus).

## Replay on real history
`python -m scripts.replay` - rough check of the rules on the last ~1000 candles.

## Setup on the VPS

1. Create a bot with @BotFather and copy the token.
2. Set `TELEGRAM_BOT_TOKEN` in the project's environment (hPanel Docker manager).
   Leave `TELEGRAM_CHAT_ID` empty, start the bot, message it once, then read your chat id
   from the logs (`message from chat id ...`), set it and restart.
3. Log Claude in once (browser login, uses your subscription):
   `docker exec -it trading-bot claude` then `/login`, then `/exit`.
4. Check: send `/status` in Telegram.

## Judge it honestly
Run 4-6 weeks. Only consider real money if `/stats` shows 30+ closed trades with
positive expectancy after fees. Until then this is a learning project.

## Tests
`pip install -r requirements.txt pytest && python -m pytest -q`
