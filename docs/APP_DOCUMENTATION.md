# AutoTrade Hub — Full App Documentation
**Generated:** 2026-05-02 | **Version:** Current (PostgreSQL + Freqtrade v2026.3)

---

## 1. App Overview

AutoTrade Hub is a full-stack AI-powered crypto trading platform built on:
- **Frontend:** Next.js 14 (TypeScript) — http://localhost:3000
- **Backend:** FastAPI (Python 3.14) — http://localhost:8000
- **Database:** PostgreSQL 17 (local) via psycopg v3 + SQLAlchemy 2.0
- **Trading Engine:** Freqtrade v2026.3 (paper + live trading)
- **Exchange:** KuCoin (REST + WebSocket)
- **Auth:** Clerk JWT (multi-user, per-user data isolation)

---

## 2. Feature Map

```
AutoTrade Hub
├── /setup              → 4-step wizard (KuCoin keys, OpenRouter AI, risk limits, Telegram)
├── /                   → Dashboard (live ticker, signals panel, portfolio overview)
├── /strategy/upload    → Upload PDF/DOCX/text → AI converts to Freqtrade Python strategy
├── /strategy/templates → 6 pre-built strategies (no AI needed)
├── /strategy/editor    → Edit + validate strategy code in-browser
├── /opportunities      → Market scanner: 20 pairs × 4 strategies, scored live
├── /backtest           → Historical backtesting with 1M–10Y presets
├── /paper-trade        → Simulated trading with virtual money (live market data)
├── /live               → Real live trading (KuCoin API, requires confirmation)
└── /trade/history      → Trade history, P&L, audit log
```

---

## 3. Setup Wizard (`/setup`)

**Steps:**

| Step | What it does |
|------|-------------|
| 1 | KuCoin API key, secret, passphrase — test connection button shows USDT balance |
| 2 | OpenRouter AI key (free) — test + browse 8+ free models |
| 3 | Risk sliders — max position size (1–20%), max open trades (1–10), daily drawdown (1–15%), stop-loss (1–10%) |
| 4 | Telegram bot token + chat ID (optional, for trade alerts) |

**Default AI Model:** `nvidia/nemotron-3-super-120b-a12b:free` (Nemotron 3 Super 120B — recommended, 11–22s)

---

## 4. Strategy Upload / AI Converter (`/strategy/upload`)

### What it does
Accepts trading strategy descriptions in any format (plain English, PDF, DOCX, TXT) and converts them to valid Freqtrade `IStrategy` Python code using an LLM.

### Modes
| Mode | Description |
|------|-------------|
| Upload Document | Drag & drop PDF, DOCX, TXT, MD — AI parses and codes it |
| Type It Out | Write rules directly in a text box |
| Use a Template | Pick from 6 pre-built strategies (no AI needed) |

### AI Models Available (Free, via OpenRouter)
| Model | Speed | Quality |
|-------|-------|---------|
| Nemotron 3 Super 120B ⭐ | 11–22s | Best overall |
| GPT-OSS 120B | Fast | Very good |
| GPT-OSS 20B | Fastest | Good |
| Gemma 4 31B | Medium | Good |
| Qwen3 Coder | Medium | Best for code |
| Llama 3.3 70B | Medium | Good |
| Hermes 3 405B | Slow | Powerful |

### Validation
Every generated strategy goes through local AST validation checking:
- Inherits from `IStrategy`
- Has `populate_indicators`, `populate_entry_trend`, `populate_exit_trend` methods
- Has `stoploss` defined
- No dangerous imports (os.system, subprocess, etc.)

### Test Result (Live)
```
Input:  "Buy when RSI drops below 30. Sell when RSI goes above 70. Stoploss 3%."
Output: valid=True, errors=[]  ← Local validator confirms IStrategy structure
Model:  Nemotron 3 Super 120B (~22s)
```

---

## 5. Strategy Templates (`/strategy/templates`)

6 pre-built, production-ready Freqtrade strategies:

| Strategy | Description | Best Timeframe |
|----------|-------------|----------------|
| **EmaScalpingStrategy** | EMA crossover scalping, rides strong trends | 5m, 15m |
| **MacdCrossoverStrategy** | MACD signal-line crosses, catches momentum turns | 15m, 1h |
| **RsiBollingerStrategy** | Mean reversion from Bollinger Band extremes | 15m, 1h |
| **DcaAccumulationStrategy** | Dollar-cost averaging on weakness | 1h, 4h |
| **MissCandleLongStrategy** | Pattern-based long entry on missed candles | 15m |
| **MissCandleShortStrategy** | Pattern-based short on missed candles | 15m |

---

## 6. Strategy Editor (`/strategy/editor`)

- Monaco-style code editor with syntax highlighting
- Real-time local validation (AST + safety checks)
- AI assist: describe a change in plain English → AI modifies the code
- Save to database + writes to `strategies/user_generated/<user_id>/strategy_<id>.py`

---

## 7. Opportunities Scanner (`/opportunities`)

### Live Test Results (2026-05-02)
```
Scan parameters: 15m timeframe, 20 pairs, 4 strategies
Scanned: 20 pairs | Failed: 0 | Time: ~8s

Top 3 opportunities found:
#1 INJ/USDT  — EMA Scalping     score=82  STRONG_BUY  Entry=88  Fit=76
#2 FIL/USDT  — RSI + Bollinger  score=81  STRONG_BUY  Entry=83  Fit=79
#3 TRX/USDT  — MACD Crossover   score=78  STRONG_BUY  Entry=81  Fit=75
```

### Scoring System
```
Overall Score (0–100) = 55% × Entry Quality + 45% × Fit Score

Entry Quality:  Is NOW a good entry? (RSI, MACD, Bollinger, volume)
Fit Score:      Does this pair's market regime match this strategy?
Confidence:     0–1 based on how many indicators returned valid data
```

### Recommendations
| Score | Label | Meaning |
|-------|-------|---------|
| ≥75 | STRONG_BUY | Excellent setup — all signals aligned |
| 60–74 | BUY | Good setup — most signals positive |
| 45–59 | HOLD | Neutral — no clear edge |
| <45 | AVOID | Poor conditions for this strategy |

### Actions on Each Card
- **📄 Paper Trade** — one-click to paper trade with pre-filled pair/strategy/timeframe
- **🔴 Live Trade** — one-click to live trade
- **📋 Copy Signal** — copies formatted signal text to clipboard (for Telegram/WhatsApp/Discord)

### Copied Signal Format
```
🟢 STRONG BUY — INJ/USDT (15m)
📊 Strategy: EMA Scalping
🏆 Score: 82/100  |  Entry: 88  |  Fit: 76  |  Confidence: 91%
💰 Expected profit: +1.23% (historical)
📈 RSI: 28.4 | MACD: 0.0012 | ADX: 38.1
💡 RSI oversold — strong entry signal • Trend aligned with EMA
🕒 Fri, 02 May 2026 22:00:00 UTC
📱 Sent via AutoTrade Hub
```

---

## 8. Backtesting (`/backtest`)

### Historical Period Presets
| Preset | Days | Data Download (15m) | Use Case |
|--------|------|---------------------|----------|
| 1M | 30 | Instant (cached) | Quick test |
| 3M | 90 | Instant | Recent performance |
| 6M | 180 | Instant | Medium-term |
| **1Y** | 365 | **Instant** | **Recommended** |
| 2Y | 730 | ~30s first run | Long-term |
| 5Y | 1825 | ~2 min first run | Deep historical |
| 10Y | 3650 | ~5 min first run | Full cycle |
| Custom | Manual | Varies | YYYYMMDD-YYYYMMDD |

> Data is cached locally in feather format. After first download, all subsequent backtests on the same pair/timeframe are instant.

### How it works
1. Freqtrade downloads real OHLCV data from KuCoin API (paginated, 1,500 candles/request)
2. Runs your strategy code against the historical candles
3. Returns: total profit %, win rate, max drawdown, Sharpe ratio, all individual trades

### Metrics Shown
| Metric | Description |
|--------|-------------|
| Total Profit % | Overall portfolio return |
| Win Rate | % of trades that were profitable |
| Max Drawdown | Largest peak-to-trough drop |
| Sharpe Ratio | Risk-adjusted return (>1 = good, >2 = excellent) |
| Total Trades | Number of completed trades |
| Avg Duration | Average trade hold time |

### Verified Accuracy (Real Test Run)
```
Strategy: EmaScalpingStrategy
Pair:     BTC/USDT
Period:   1h timeframe (historical)
Result:   83 trades, +0.31% total profit
Win rate: ~52%
```

### Charts
- **Equity Curve** — portfolio value over each trade
- **Profit Distribution** — green/red bar chart per trade
- **Trade Table** — every trade with open/close price, P&L, dates, exit reason

---

## 9. TradingView Signals Panel

Available on: Dashboard, Paper Trade page, Live Trade page

### Live Signal (BTC/USDT 15m — tested 2026-05-02)
```
Recommendation: BUY
Buy indicators:    12
Neutral:           4
Sell:              1

Key indicators:
  RSI(14):    50.1
  MACD:      -31.55
  BB Upper:  78,701
  BB Lower:  77,875
  ADX:        22.8
Source: kucoin_klines (live data)
```

### Action Buttons
| Button | What it does |
|--------|-------------|
| ⚡ Buy Now | Opens modal → choose Paper or Live trade → navigates with pair pre-filled |
| 🤖 Auto-Buy | Toggles the auto-trade engine for this pair/interval |
| 📋 Copy Signal | Copies formatted signal text to clipboard |

---

## 10. Paper Trading (`/paper-trade`)

### How it works
- Freqtrade runs in `dry_run: true` mode with a virtual wallet (default $1,000)
- Uses **live real-time KuCoin market data** (WebSocket feed)
- Executes strategy logic on live candlestick data
- No real money involved — all trades are simulated

### Configuration
- Pair(s) to trade
- Timeframe (5m, 15m, 30m, 1h, 4h, 1d)
- Starting virtual balance (USDT)
- Stop-loss %
- Max open trades
- TradingView chart embedded in-page

### Controls
- **Start** — launches Freqtrade subprocess in paper mode
- **Stop** — gracefully stops the bot
- **Emergency Stop** — halts all activity and closes all open positions

---

## 11. Live Trading (`/live`)

### Safety Checks (Required before live trading)
1. Type `CONFIRM` in the confirmation box
2. System checks:
   - KuCoin API keys configured and valid
   - Max open trades set
   - Max position size set
   - Max daily drawdown set
   - Override safety toggle (acknowledge warnings)

### Risk Controls (from setup)
| Control | Default | Max |
|---------|---------|-----|
| Max position size | 5% | 20% |
| Max open trades | 3 | 10 |
| Daily drawdown limit | 5% | 15% |
| Stop-loss | 3% | 10% |
| Trailing stop | 0% (off) | configurable |
| Take profit | 0% (off) | configurable |

---

## 12. Auto-Trade Engine

Autonomous trading: scanner → decision → deploy. Zero manual intervention.

### Configuration
| Setting | Default | Description |
|---------|---------|-------------|
| Enabled | false | Toggle on/off |
| Mode | paper | paper or live |
| Min score | 70 | Only trade opportunities above this score |
| Timeframe | 15m | Scan timeframe |
| Scan interval | 600s | Re-scan every N seconds |
| Strategy | auto | Pin to a specific strategy, or auto-select best |
| Pairs | auto | All 20 default pairs, or custom list |

### Decision Loop (every 10 min by default)
```
1. Run opportunity scanner on all configured pairs
2. Find best (pair × strategy) with score > min_score
3. If better than current running strategy → rotate
   (only if no open positions to avoid cutting losses)
4. Deploy Freqtrade with selected strategy
5. Log action to audit trail
```

---

## 13. Market Data

| Endpoint | Description |
|----------|-------------|
| `/api/market/pairs` | All KuCoin USDT pairs (100+) |
| `/api/market/signals/{pair}` | Live TradingView TA signal |
| `/api/analysis/top-volume` | Top 50 pairs by 24h volume |
| `/api/analysis/opportunities` | Full scanner results |
| `/api/analysis/analyze/{pair}` | Per-pair strategy scoring |

### Live Volume Leaders (tested 2026-05-02)
```
#1 BTC/USDT  — $204M 24h vol
#2 ETH/USDT  — $187M 24h vol
#3 XRP/USDT  — $97M  24h vol
```

---

## 14. Backend API — All Endpoints

### Public (no auth)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | App health + active users |
| GET | `/api/strategy/templates` | 6 pre-built strategies |
| POST | `/api/strategy/validate` | Validate strategy code |
| GET | `/api/market/signals/{pair}` | Live TA signal |
| GET | `/api/analysis/universe` | Default pairs + strategies |
| GET | `/api/analysis/opportunities` | Scanner results |
| GET | `/api/analysis/top-volume` | Volume leaderboard |
| GET | `/api/analysis/analyze/{pair}` | Per-pair scores |

### Auth-Required (Clerk JWT Bearer token)
| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/config/setup` | Save API keys + preferences |
| GET | `/api/config/test-kucoin` | Test KuCoin connection |
| GET | `/api/config/test-openrouter` | Test OpenRouter AI |
| GET | `/api/config/models` | List available AI models |
| POST | `/api/strategy/upload` | AI strategy converter |
| POST | `/api/strategy/parse` | Re-parse strategy text |
| POST | `/api/strategy/ai-assist` | AI code modification |
| GET | `/api/strategy/list` | User's strategies |
| GET | `/api/strategy/{id}` | Single strategy |
| PUT | `/api/strategy/{id}` | Update strategy |
| DELETE | `/api/strategy/{id}` | Delete strategy |
| POST | `/api/backtest/run` | Run backtest |
| POST | `/api/backtest/bulk` | Run across multiple pairs |
| GET | `/api/backtest/results/{id}` | Get saved backtest |
| POST | `/api/trade/start` | Start paper/live trading |
| POST | `/api/trade/stop` | Stop trading |
| GET | `/api/trade/status` | Bot status |
| GET | `/api/trade/open` | Open trades |
| GET | `/api/trade/history` | Closed trades |
| POST | `/api/trade/force-close/{id}` | Force close a trade |
| POST | `/api/trade/emergency-stop` | Stop everything |
| GET | `/api/trade/audit` | Audit log |
| GET | `/api/autotrade/status` | Engine status |
| POST | `/api/autotrade/start` | Start engine |
| POST | `/api/autotrade/stop` | Stop engine |
| GET/PUT | `/api/autotrade/settings` | Engine configuration |
| GET | `/api/market/pairs` | All trading pairs |

---

## 15. Full Test Results Summary (2026-05-02)

| Test | Result | Detail |
|------|--------|--------|
| Backend health | ✅ PASS | `{"status":"healthy"}` |
| Strategy validator | ✅ PASS | `valid=true, errors=[]` |
| Strategy templates | ✅ PASS | 6 templates returned |
| Live BTC signal | ✅ PASS | `rec=BUY, RSI=50.1` |
| Universe | ✅ PASS | 20 pairs, 4 strategies |
| Opportunities scanner | ✅ PASS | 20/20 pairs scanned, 0 failed |
| Top volume | ✅ PASS | 50 pairs returned |
| Auth gating | ✅ PASS | All 5 auth endpoints return 401 |
| MATIC/USDT bug | ✅ FIXED | Replaced with POL/USDT |
| Frontend port 3000 | ✅ PASS | HTTP 200 on /sign-in |
| PostgreSQL | ✅ PASS | All 6 tables, 3 migrations applied |
| psycopg driver | ✅ PASS | v3.3.3 installed |

**Overall: 12/12 tests PASS**

---

## 16. Known Limitations

| Limitation | Detail | Workaround |
|------------|--------|-----------|
| `expected_profit_pct` null | Shows once backtests have been run and saved | Run a backtest first |
| TradingView rate limit | ~3 req/s limit — scanner backs off with cached data | Built-in cooldown + stale cache |
| Data download time (5Y+) | First-run 15m data download takes 2–5 min | Subsequent runs use local cache |
| Live trading (KuCoin only) | Only supports KuCoin exchange | — |
| AI conversion quality | Depends on free model availability/load | Switch model in Setup if one is slow |

---

## 17. Database Schema

```
PostgreSQL 17 @ localhost:5432/autotrade

Tables:
  config          — user API keys (encrypted), risk settings, preferences
  strategies      — uploaded/generated strategy code + metadata
  trades          — all trade records (paper + live)
  backtests       — backtest runs + metrics + results JSON
  trade_audit     — immutable audit log of all trading actions
  alembic_version — migration tracking
```

---

## 18. Security

- All KuCoin API keys encrypted at rest using `sha256(APP_SECRET_KEY + '\0' + user_id)` per-user key
- Clerk JWT validates every auth-gated request
- Per-user data isolation at DB query level (every query filters by `user_id`)
- Per-user Freqtrade process isolation (separate configs, data dirs, PIDs)
- Rate limiting on all AI + trading endpoints (SlowAPI)
- Live trading requires explicit `"CONFIRM"` text + optional safety override
- Full audit trail: every trade action logged with timestamp + IP

---

*Document generated by AutoTrade Hub automated testing suite.*
