"""v2_futures_copy_trading_multi_strategy

Adds columns and tables for:
  - Futures trading (leverage, liquidation_price, market_type on trades)
  - Copy trading (copy_signals, copy_subscriptions tables)
  - Multi-strategy (strategy_instances table)
  - Copy trading opt-in on strategies (allow_copy_trading, default_leverage)

Revision ID: c8d9e0f1a2b3
Revises: ab7b6388ba81
Create Date: 2026-05-07 06:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'ab7b6388ba81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Nuclear idempotent approach — raw PostgreSQL IF NOT EXISTS.
    These statements CANNOT raise DuplicateColumn regardless of how many times
    they run or what state the DB is already in.
    """
    from sqlalchemy import text

    # ── trades: add futures columns ───────────────────────────────────────────
    alter_stmts = [
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS market_type TEXT DEFAULT 'spot'",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS leverage INTEGER DEFAULT 1",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS liquidation_price FLOAT",
        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS copy_source_id INTEGER",
        # ── strategies: copy-trading opt-in ───────────────────────────────────
        "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS allow_copy_trading BOOLEAN DEFAULT false",
        "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS default_leverage INTEGER DEFAULT 1",
    ]
    for stmt in alter_stmts:
        try:
            op.execute(text(stmt))
        except Exception:
            pass  # already exists or table doesn't exist — safe to ignore

    # ── strategy_instances: multi-strategy per user ───────────────────────────
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS strategy_instances (
            id              SERIAL PRIMARY KEY,
            user_id         TEXT NOT NULL,
            strategy_id     INTEGER REFERENCES strategies(id),
            strategy_name   TEXT NOT NULL,
            market_type     TEXT DEFAULT 'spot',
            mode            TEXT DEFAULT 'paper',
            pairs           TEXT DEFAULT 'BTC/USDT',
            leverage        INTEGER DEFAULT 1,
            timeframe       TEXT DEFAULT '15m',
            stoploss        FLOAT DEFAULT -0.03,
            takeprofit      FLOAT DEFAULT 0.015,
            wallet          FLOAT DEFAULT 1000.0,
            risk_pct        FLOAT DEFAULT 5.0,
            is_running      BOOLEAN DEFAULT false,
            engine_key      TEXT UNIQUE,
            total_trades    INTEGER DEFAULT 0,
            total_pnl       FLOAT DEFAULT 0.0,
            created_at      TIMESTAMP DEFAULT NOW(),
            updated_at      TIMESTAMP DEFAULT NOW()
        )
    """))
    try:
        op.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_strategy_instances_user_id ON strategy_instances(user_id)"
        ))
    except Exception:
        pass

    # ── copy_signals ──────────────────────────────────────────────────────────
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS copy_signals (
            id              SERIAL PRIMARY KEY,
            master_user_id  TEXT NOT NULL,
            pair            TEXT NOT NULL,
            direction       TEXT DEFAULT 'long',
            market_type     TEXT DEFAULT 'spot',
            leverage        INTEGER DEFAULT 1,
            entry_price     FLOAT,
            sl_price        FLOAT,
            tp_price        FLOAT,
            stake_pct       FLOAT DEFAULT 5.0,
            strategy_name   TEXT,
            signal_type     TEXT DEFAULT 'entry',
            profit_pct      FLOAT,
            profit_abs      FLOAT,
            broadcasted_at  TIMESTAMP DEFAULT NOW(),
            expires_at      TIMESTAMP,
            closed_at       TIMESTAMP
        )
    """))
    try:
        op.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_copy_signals_master_user_id ON copy_signals(master_user_id)"
        ))
    except Exception:
        pass

    # ── copy_subscriptions ────────────────────────────────────────────────────
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS copy_subscriptions (
            id                  SERIAL PRIMARY KEY,
            follower_user_id    TEXT NOT NULL,
            master_user_id      TEXT NOT NULL,
            is_active           BOOLEAN DEFAULT true,
            copy_mode           TEXT DEFAULT 'paper',
            copy_market_type    TEXT DEFAULT 'spot',
            max_leverage        INTEGER DEFAULT 10,
            stake_override_pct  FLOAT,
            total_copied        INTEGER DEFAULT 0,
            total_profit        FLOAT DEFAULT 0.0,
            win_count           INTEGER DEFAULT 0,
            created_at          TIMESTAMP DEFAULT NOW()
        )
    """))
    try:
        op.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_copy_subscriptions_follower ON copy_subscriptions(follower_user_id)"
        ))
        op.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_copy_subscriptions_master ON copy_subscriptions(master_user_id)"
        ))
    except Exception:
        pass


def downgrade() -> None:
    from sqlalchemy import text
    for stmt in [
        "DROP TABLE IF EXISTS copy_subscriptions",
        "DROP TABLE IF EXISTS copy_signals",
        "DROP TABLE IF EXISTS strategy_instances",
        "ALTER TABLE strategies DROP COLUMN IF EXISTS default_leverage",
        "ALTER TABLE strategies DROP COLUMN IF EXISTS allow_copy_trading",
        "ALTER TABLE trades DROP COLUMN IF EXISTS copy_source_id",
        "ALTER TABLE trades DROP COLUMN IF EXISTS liquidation_price",
        "ALTER TABLE trades DROP COLUMN IF EXISTS leverage",
        "ALTER TABLE trades DROP COLUMN IF EXISTS market_type",
    ]:
        try:
            op.execute(text(stmt))
        except Exception:
            pass
