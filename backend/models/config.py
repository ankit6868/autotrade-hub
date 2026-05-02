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
    # --- Notifications ---
    telegram_token = Column(Text)
    telegram_chat_id = Column(Text)
    discord_webhook_url = Column(Text, nullable=True)   # Discord channel webhook URL
    # --- Webhook (inbound signals from TradingView / external) ---
    webhook_secret = Column(Text, nullable=True)        # URL-safe token for /api/webhook/signal
    created_at = Column(DateTime, server_default=func.now())


Index("ix_config_user_id_unique", Config.user_id, unique=True)
