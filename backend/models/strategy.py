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
    # NICE-8: auto_trade_enabled / auto_trade_mode / allow_copy_trading
    # columns were dropped after the spot purge — the engines that wrote
    # them (autotrade_engine, copy_trading) are gone. Migration
    # 20260523_a1b2c3d4e5f6 drops the columns where the DB supports it
    # (Postgres + SQLite ≥ 3.35); older SQLite is best-effort.
    default_leverage   = Column(Integer, default=1,     server_default='1',     nullable=True)
    take_profit        = Column(Float,   default=0.015, server_default='0.015', nullable=True)  # e.g. 0.015 = 1.5%
    # ── Phase 5 — Structured strategy template (PDF §8) ─────────────────
    # JSON of the decoded + resolved StrategyTemplate (rules with roles,
    # risk plan, confidence score, missing/inferred fields, conflicts).
    # Populated by strategy_validator.validate_and_score after upload and
    # refreshed when the user edits the code. The bot create endpoint reads
    # this to enforce the live guardrail (block live unless confidence ≥ 85
    # and no critical missing fields).
    compiled_template       = Column(JSON, nullable=True)
    confidence_score        = Column(Integer, default=0)
    live_permission         = Column(Text,    default="blocked")   # live_eligible | demo_only | backtest_only | blocked
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
