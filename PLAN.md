# AI Trading App — Plan (Indian stocks, NSE/BSE)

Web dashboard that monitors NSE/BSE stocks, runs algorithmic strategies and auto-trades,
with strict risk controls.

## Safety
- Paper mode by default (simulated fills with Zerodha-like costs + slippage).
- Live mode requires `TRADING_MODE=live`, Kite API keys, and `LIVE_TRADING_CONFIRM=I_UNDERSTAND_THE_RISKS`.
- Risk manager: ~1% equity risk per trade, max 20% per stock, max 5 positions, 2% daily loss halt,
  1.5% stop-loss / 3% take-profit, 30-min re-entry cooldown, market hours only (09:15–15:30 IST),
  MIS auto square-off at 15:15. Kill switch flattens everything. Long-only.

## Modules
| Module | Purpose |
|---|---|
| config.py | Settings from `.env` |
| indicators.py | EMA, RSI, MACD, ATR, Bollinger |
| strategies.py | EMA cross, RSI reversion, MACD momentum, ensemble score (-1..+1) |
| data.py | Simulated / Yahoo Finance / Kite data feeds |
| brokers.py | PaperBroker, KiteBroker |
| risk.py | Position sizing and limits |
| engine.py | Trading loop: candles → signals → exits → risk-checked entries → orders |
| store.py | SQLite: trades, signals, equity |
| backtest.py | Historical strategy testing |
| main.py | FastAPI REST + WebSocket + Kite login flow |
| static/ | Dashboard: KPIs, watchlist, chart, positions, trades, equity curve, backtest |

## Build order
1. Indicators + strategies  2. Risk + paper broker  3. Data feeds  4. Engine + storage
5. API  6. Dashboard  7. Backtester  8. Kite live  9. Tests + README

## Open decisions
1. Broker: Zerodha Kite (default) or other?
2. Style: intraday MIS (default) or delivery/swing CNC?
3. Capital/risk: ₹1,00,000 paper, 1% per trade (default)?
4. Watchlist: RELIANCE, TCS, INFY, HDFCBANK, ICICIBANK (default)?
