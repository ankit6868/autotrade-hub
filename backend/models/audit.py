"""
Append-only audit trail of every significant trading action.

Used to answer "who did what when" for compliance and incident response.
Never UPDATE or DELETE rows — only INSERT.
"""
from sqlalchemy import Column, Integer, Text, DateTime, JSON, Index
from sqlalchemy.sql import func

from .database import Base


class TradeAudit(Base):
    __tablename__ = "trade_audit"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    # 'trade.start' | 'trade.stop' | 'trade.force_close' | 'trade.emergency_stop'
    # | 'autotrade.deploy' | 'config.update' | 'strategy.create'
    event = Column(Text, nullable=False, index=True)
    mode = Column(Text)         # 'paper' | 'live' | None
    strategy_id = Column(Integer)
    pair = Column(Text)
    # Free-form structured payload — serialised request body, broker response,
    # etc. Use JSON column type so Postgres stores it natively and SQLite
    # falls back to TEXT.
    payload = Column(JSON)
    actor_ip = Column(Text)
    user_agent = Column(Text)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)


Index("ix_trade_audit_user_event_time", TradeAudit.user_id, TradeAudit.event, TradeAudit.created_at.desc())
