from sqlalchemy import Column, Integer, Text, Float, DateTime, ForeignKey, CheckConstraint, Index
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
    mode = Column(Text, nullable=False)
    pair = Column(Text, nullable=False)
    side = Column(Text, default="long")
    entry_price = Column(Float)
    exit_price = Column(Float)
    amount = Column(Float)
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
