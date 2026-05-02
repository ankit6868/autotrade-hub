from sqlalchemy import Column, Integer, Text, Float, Boolean, DateTime, JSON, Index
from sqlalchemy.sql import func
from .database import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, default="local-dev", server_default="local-dev", index=True)
    name = Column(Text, nullable=False)
    description = Column(Text)
    original_text = Column(Text)
    generated_code = Column(Text, nullable=False)
    model_used = Column(Text)
    indicators = Column(JSON)
    timeframe = Column(Text, default="15m")
    pairs = Column(JSON)
    stoploss = Column(Float, default=-0.03)
    is_template = Column(Boolean, default=False)
    # --- Auto-trade enablement per strategy ---
    auto_trade_enabled = Column(Boolean, default=False)
    auto_trade_mode = Column(Text, default="paper")  # 'paper' | 'live'
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
