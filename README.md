# AI Trader: algorithmic trading for NSE/BSE

A phone-first web app (designed for the iPhone 15 Pro Max) that watches Indian stocks, auto-trades
intraday with algorithms, and lets you trade by hand with Kite-style controls. It works with
**Zerodha Kite Connect**.

> ⚠️ **No strategy can guarantee profit.** The app limits losses with stop-losses and daily limits, but
> some trades *will* lose. Run it in **paper mode** for a few weeks and check the backtests before
> risking real money. You are responsible for every order it places.

## Features

**Auto-trading bot (intraday / MIS)**
- Strategies: EMA crossover (trend), MACD (momentum), RSI (mean reversion), and an **ensemble**
  that blends them into a score from -1 to +1. Buys are only allowed above the 50 EMA, so the bot trades with the trend.
- Position size is set so that a stopped-out trade loses about 1% of the bot's capital.

**Loss-cutting and profit protection** (applies to bot trades and any manual trade marked 🛡 *Auto-protect*)
| Rule | Default |
|---|---|
| Hard stop-loss | 1.5% below entry |
| Move stop to breakeven | once a trade is up 1% |
| Trailing stop | 1% below the best price reached |
| Early exit | losing trade **and** signal turns bearish |
| Stale-trade exit | flat or losing after 120 min |
| Take-profit | +3% |
| Daily loss limit | bot halts and closes its positions at -2% for the day |
| Profit lock | after +1.5% on the day, halt if half of the peak is given back |
| Time rules | no new trades after 14:45, intraday square-off at 15:15 |
| **Kill switch** | cancels all orders, closes all intraday positions, stops the bot |

All of these can be changed in the app under **Bot → Strategy, risk & loss-cutting rules**.

**Manual trading, Kite style**
- BUY/SELL on NSE/BSE; Intraday (MIS) or Longterm (CNC).
- Order types: Market, Limit, SL and SL-M. Validity: Day or IOC.
- Modify or cancel open orders.
- Positions (Exit / Add / Protect), Holdings, Order book, Trade history.
- Intraday short selling.

**Funds**
- Paper mode: add or withdraw **any amount**, with a ledger.
- Live mode: shows your real Kite margin. Add money through the Zerodha app.
- **Bot capital** caps how much the bot may use. Your manual trades can use the rest.

**Also included**
- Candlestick charts with EMA lines and buy/sell markers.
- Backtester (win rate, profit factor, max drawdown).
- Zerodha brokerage, STT, stamp duty and GST estimates applied in paper mode.

## Run it

```bash
./run.sh                 # creates .venv, installs, copies .env.example -> .env, starts on port 8000
```

Then open **http://localhost:8000** in your browser.

**On your iPhone:**
1. Set `APP_PASSWORD=` in `.env`, then restart the app.
2. Keep the computer and the phone on the same Wi-Fi.
3. Open `http://<computer-IP>:8000` in Safari.
4. Tap **Share → Add to Home Screen**. The app then opens full-screen like a native app.

To get the computer's IP address: on macOS run `ipconfig getifaddr en0`; on Windows run `ipconfig`.

Run the tests with `.venv/bin/python -m pytest`.

## Going from demo → paper trading on real prices → live

1. **Demo (default):** `DATA_SOURCE=simulated` uses fake prices, so you can try every button any time.
2. **Paper trading on real prices:** set `DATA_SOURCE=yahoo` (free, about 15 min delayed), or `kite`
   (real time; needs the Kite Connect subscription with historical data).
3. **Live trading on Zerodha:**
   1. Create an app at <https://developers.kite.trade>. Set the redirect URL to `http://<your-computer>:8000/kite/callback`.
   2. Put the keys in `.env`:
      ```
      KITE_API_KEY=...
      KITE_API_SECRET=...
      DATA_SOURCE=kite
      TRADING_MODE=live
      LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS
      ```
   3. Restart the app. Then open **Account → Login to Kite**. Kite tokens expire every morning, so you log in once per day.
   4. A red **LIVE** badge shows at the top. Every live order and bot start asks you to confirm.
   5. Start with a small **Bot capital**.

## Layout

```
app/
  config.py      settings (.env)
  indicators.py  EMA, RSI, MACD, ATR, Bollinger
  strategies.py  ema_cross, macd, rsi, ensemble
  risk.py        sizing, entry checks, stop/breakeven/trailing/early exits, daily halts
  brokers.py     PaperBroker (simulated Kite) and KiteBroker (live)
  data.py        simulated / Yahoo / Kite price feeds
  engine.py      price loop, strategy loop, protection, kill switch
  backtest.py    historical test with the same rules
  store.py       SQLite (trades, equity, events, state)
  main.py        FastAPI REST + WebSocket + Kite login + optional password
  static/        phone-first web app (installable to home screen)
tests/           pytest suite
```

Limits to know about:
- NSE holidays are not built in. The app assumes Mon–Fri, 09:15–15:30 IST, and Kite rejects orders on holidays anyway.
- Only equity (cash market) is supported. F&O is not.
- In live mode, Kite also auto-squares MIS positions at around 15:20.
