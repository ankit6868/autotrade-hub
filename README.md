# AutoTrade Hub

AI-powered crypto trading platform. Upload your strategy, backtest it, paper trade, then go live — all for free.

## Quick Start

### Prerequisites
- Python 3.11+
- Node.js 18+
- (Optional) Freqtrade installed for backtesting/live trading

### 1. Clone and set up environment

```bash
cp .env.example .env
# Edit .env with your APP_SECRET_KEY (any random 32+ char string)
```

### 2. Start the backend

```bash
pip install -r requirements.txt
uvicorn backend.main:app --reload --port 8000
```

### 3. Start the frontend

```bash
cd frontend
npm install
npm run dev
```

### 4. Open the app

Go to [http://localhost:3000](http://localhost:3000) and complete the Setup Wizard.

## Get Your Free API Keys

### KuCoin (for trading)
1. Create account at [kucoin.com](https://www.kucoin.com)
2. Go to **Account > API Management**
3. Create API key with **Trade** permission (never enable Withdraw)
4. Save your Key, Secret, and Passphrase

### OpenRouter (for AI strategy parsing)
1. Go to [openrouter.ai](https://openrouter.ai)
2. Sign up (Google/GitHub login works)
3. Go to **Keys > Create Key**
4. Copy the key — no credit card needed, 100% free

## Features

- **Strategy Upload** — Paste a PDF/DOCX/TXT of your trading rules, AI converts it to code
- **Strategy Editor** — Monaco code editor with AI assist for modifications
- **4 Pre-built Templates** — RSI+Bollinger, MACD Crossover, EMA Scalping, DCA
- **Backtesting** — Test against historical KuCoin data with full metrics
- **Paper Trading** — Dry-run with virtual money on live market data
- **Live Trading** — Real trading with safety gates (7-day paper requirement, kill switch)
- **Analytics** — Full trade history with charts, CSV export, strategy comparison

## Safety Features

1. API key encryption (Fernet) at rest
2. 7-day paper trading requirement before live
3. Emergency stop button on every page
4. Daily drawdown auto-stop
5. Position size limits
6. Max open trades cap
7. "CONFIRM" typed confirmation for live trading
8. Never enable withdrawal API permissions

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Frontend | Next.js 14, React, Tailwind CSS, Recharts |
| Backend | FastAPI (Python) |
| AI | OpenRouter (free models) |
| Trading | Freqtrade |
| Market Data | KuCoin API |
| Signals | TradingView TA |
| Database | SQLite |
| Editor | Monaco Editor |

## Docker

```bash
docker-compose up --build
```

Frontend: http://localhost:3000
Backend: http://localhost:8000
