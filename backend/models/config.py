from sqlalchemy import Column, Integer, Text, Float, Boolean, DateTime, Index
from sqlalchemy.sql import func
from .database import Base


class Config(Base):
    __tablename__ = "config"

    id = Column(Integer, primary_key=True)
    # Clerk user id (sub claim). 'local-dev' when Clerk isn't configured.
    user_id = Column(Text, index=True, nullable=False, default="local-dev")
    kucoin_key_enc = Column(Text)
    kucoin_secret_enc = Column(Text)
    kucoin_passphrase_enc = Column(Text)
    # Regular (normal) KuCoin Futures API keys — used by the /regular-futures-trade
    # terminal. Separate from the Lead copy-trading keys above so a user can
    # connect a normal futures key independently. Null when not configured.
    kucoin_reg_key_enc = Column(Text)
    kucoin_reg_secret_enc = Column(Text)
    kucoin_reg_passphrase_enc = Column(Text)
    openrouter_key_enc = Column(Text)
    preferred_model = Column(Text, default="nvidia/nemotron-3-super-120b-a12b:free")
    max_position_pct = Column(Float, default=5.0)
    max_open_trades = Column(Integer, default=3)
    max_daily_drawdown_pct = Column(Float, default=5.0)
    default_stoploss_pct = Column(Float, default=3.0)
    # --- Advanced trading controls ---
    trailing_stop_pct = Column(Float, default=0.0)            # 0 disables trailing
    take_profit_pct = Column(Float, default=0.0)              # 0 disables static TP
    position_adjustment = Column(Boolean, default=False)      # enable DCA / pyramiding
    # --- Auto-trade engine ---
    auto_trade_enabled = Column(Boolean, default=False)
    auto_trade_mode = Column(Text, default="paper")           # 'paper' or 'live'
    auto_trade_min_score = Column(Float, default=70.0)        # 0..100 threshold
    auto_trade_timeframe = Column(Text, default="15m")
    auto_trade_scan_interval_s = Column(Integer, default=600) # scan every 10 min
    # Optional pin: null = auto-select best from scanner
    auto_trade_strategy_id = Column(Integer, nullable=True)   # FK to strategies.id
    auto_trade_pairs = Column(Text, nullable=True)            # CSV e.g. "BTC/USDT,ETH/USDT"
    # --- Auto-sell engine ---
    auto_sell_enabled = Column(Boolean, default=False)        # auto-sell on SELL/STRONG_SELL signals
    auto_sell_mode = Column(Text, default="paper")            # 'paper' or 'live'
    # --- Paper/Live bot persistence (24/7 auto-resume after container restart) ---
    bot_running = Column(Boolean, default=False)              # True if paper/live bot was running
    bot_mode = Column(Text, default="paper")                  # 'paper' or 'live'
    bot_api_mode = Column(Text, default="lead")               # 'lead' or 'regular' KuCoin futures API
    bot_strategy_name = Column(Text, nullable=True)           # e.g. 'MacdCrossoverStrategy'
    bot_pairs = Column(Text, nullable=True)                   # CSV e.g. 'BTC/USDT,ETH/USDT'
    bot_timeframe = Column(Text, default="15m")
    bot_wallet = Column(Float, default=1000.0)
    bot_stoploss = Column(Float, default=-0.03)
    # --- Notifications ---
    telegram_token = Column(Text)
    telegram_chat_id = Column(Text)
    discord_webhook_url = Column(Text, nullable=True)   # Discord channel webhook URL
    # --- Webhook (inbound signals from TradingView / external) ---
    webhook_secret = Column(Text, nullable=True)        # URL-safe token for /api/webhook/signal
    # --- Phase-NICE-4: per-TF risk config override (FR-04) ---
    # JSON map of {tf: {atr_period, sl_mult, tp_mult, min_rr}} that
    # overrides the hardcoded TIMEFRAME_CONFIG values in risk_engine.
    # When null the engine uses the defaults. Stored as TEXT (sqlite + pg
    # compatible) — risk_engine.load_user_risk_config parses + caches.
    risk_config_json = Column(Text, nullable=True)
    # --- Daily risk management for MANUAL LIVE futures trading (both terminals) ---
    # Resets every day at 00:00 UTC (= 5:30 AM IST). 0 = that limit is off.
    # When the limit is hit, new live entries are blocked and the panel shows a
    # notification; the user turns it off in Setup to resume before the reset.
    risk_daily_enabled = Column(Boolean, default=False)        # master on/off
    risk_max_trades_per_day = Column(Integer, default=0)       # max live entries/day
    risk_max_losses_per_day = Column(Integer, default=0)       # max losing closes/day
    # --- Persisted PAPER wallet (manual engine) ---
    # The paper wallet lived only in memory, so a Railway restart reset it to
    # 1000 — wiping accumulated profit/loss. Persisted here (write-through on
    # every /account poll, restored once per engine instance) so paper P&L
    # carries across restarts. _lightweight_migrate auto-adds the column.
    paper_balance = Column(Float, default=1000.0)
    created_at = Column(DateTime, server_default=func.now())


Index("ix_config_user_id_unique", Config.user_id, unique=True)
