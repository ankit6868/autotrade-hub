from sqlalchemy import Column, Integer, Text, Float, Boolean, DateTime, ForeignKey, CheckConstraint, Index
from sqlalchemy.sql import func
from .database import Base


class Backtest(Base):
    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, default="local-dev", server_default="local-dev", index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"))
    timerange = Column(Text)
    starting_balance = Column(Float, default=1000)
    total_profit = Column(Float)
    win_rate = Column(Float)
    max_drawdown = Column(Float)
    sharpe_ratio = Column(Float)
    total_trades = Column(Integer)
    avg_duration = Column(Text)
    results_json = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, default="local-dev", server_default="local-dev", index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"))
    mode = Column(Text, nullable=False)                    # paper | live
    market_type = Column(Text, default="spot")             # spot | futures
    pair = Column(Text, nullable=False)
    side = Column(Text, default="long")                    # long | short
    leverage = Column(Integer, default=1)                  # 1 = spot, >1 = futures
    liquidation_price = Column(Float)                      # futures only
    copy_source_id = Column(Integer, nullable=True)        # FK copy_signals.id
    entry_price = Column(Float)
    exit_price = Column(Float)
    amount = Column(Float)                                 # USDT stake / margin
    profit_pct = Column(Float)
    profit_abs = Column(Float)
    stoploss_price = Column(Float)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)
    exit_reason = Column(Text)
    status = Column(Text, default="open")

    __table_args__ = (
        CheckConstraint("mode IN ('paper', 'live')"),
        CheckConstraint("side IN ('long', 'short')"),
        CheckConstraint("status IN ('open', 'closed', 'cancelled')"),
    )


class StrategyInstance(Base):
    """One running engine instance — enables multi-strategy per user."""
    __tablename__ = "strategy_instances"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    strategy_name = Column(Text, nullable=False)
    market_type = Column(Text, default="spot")    # spot | futures
    mode = Column(Text, default="paper")          # paper | live
    pairs = Column(Text, default="BTC/USDT")      # CSV
    leverage = Column(Integer, default=1)
    timeframe = Column(Text, default="15m")
    stoploss = Column(Float, default=-0.03)
    takeprofit = Column(Float, default=0.0)
    wallet = Column(Float, default=1000.0)
    risk_pct = Column(Float, default=5.0)
    is_running = Column(Boolean, default=False)
    engine_key = Column(Text, unique=True)        # user_id:instance_id
    total_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class CopySignal(Base):
    """Trade signal broadcast by a master trader."""
    __tablename__ = "copy_signals"

    id = Column(Integer, primary_key=True)
    master_user_id = Column(Text, nullable=False, index=True)
    pair = Column(Text, nullable=False)
    direction = Column(Text, default="long")      # long | short
    market_type = Column(Text, default="spot")    # spot | futures
    leverage = Column(Integer, default=1)
    entry_price = Column(Float)
    sl_price = Column(Float)
    tp_price = Column(Float)
    stake_pct = Column(Float, default=5.0)
    strategy_name = Column(Text)
    signal_type = Column(Text, default="entry")   # entry | exit
    profit_pct = Column(Float)
    profit_abs = Column(Float)
    broadcasted_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime)
    closed_at = Column(DateTime)


class CopySubscription(Base):
    """Follower ➜ Master subscription."""
    __tablename__ = "copy_subscriptions"

    id = Column(Integer, primary_key=True)
    follower_user_id = Column(Text, nullable=False, index=True)
    master_user_id = Column(Text, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    copy_mode = Column(Text, default="paper")     # paper | live
    copy_market_type = Column(Text, default="spot")
    max_leverage = Column(Integer, default=10)
    stake_override_pct = Column(Float)            # null = use master's stake
    total_copied = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)
    win_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
