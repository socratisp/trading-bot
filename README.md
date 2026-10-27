# trading-bot (paper mode)

Rules-based crypto bot for Revolut X. Code finds setups and sets entry, stop,
target and size. Claude can only veto on news. You approve every trade in Telegram.
**Paper trading only. There is no live order code in this version.**

## How a trade happens
1. Every 60 min: 4h candles for BTC/ETH/SOL from Revolut X public data (no keys).
2. Setup: EMA50 > EMA200, close above EMA50, RSI 35-50 (pullback in uptrend).
3. Stop = entry - 1.5 ATR, target = entry + 3.5 ATR.
4. Risk engine: 1% equity risk, R:R >= 2 after fees, max 2 positions, max 30% per coin,
   -3% daily halt, -10% drawdown kill switch.
5. Claude (`claude -p`, Haiku) reads RSS headlines and approves or vetoes.
6. Paper phase: auto-approved, Telegram notifies every fill and exit. (Set approval.required: true for manual Approve / Reject.)
7. Paper fill at the current price; stop/target checked every 2 min.

Everything is logged to `/data/bot.sqlite`, including rejected and expired setups.

## Telegram commands
`/status` `/stats` `/resetkill`

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
