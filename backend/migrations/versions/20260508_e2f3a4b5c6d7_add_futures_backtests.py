"""add_futures_backtests_table

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-05-08

"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import text
    op.execute(text("""
        CREATE TABLE IF NOT EXISTS futures_backtests (
            id               SERIAL PRIMARY KEY,
            user_id          TEXT NOT NULL DEFAULT 'local-dev',
            strategy_id      INTEGER REFERENCES strategies(id),
            strategy_name    TEXT,
            pairs            TEXT,
            timeframe        TEXT,
            timerange        TEXT,
            leverage         INTEGER DEFAULT 10,
            starting_balance FLOAT DEFAULT 1000,
            final_balance    FLOAT,
            total_profit_pct FLOAT,
            total_profit_abs FLOAT,
            win_rate         FLOAT,
            max_drawdown     FLOAT,
            total_trades     INTEGER,
            winning_trades   INTEGER,
            losing_trades    INTEGER,
            liquidations     INTEGER DEFAULT 0,
            long_trades      INTEGER DEFAULT 0,
            short_trades     INTEGER DEFAULT 0,
            avg_leverage_pnl FLOAT,
            results_json     TEXT,
            created_at       TIMESTAMP DEFAULT NOW()
        )
    """))
    try:
        op.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_futures_backtests_user_id ON futures_backtests(user_id)"
        ))
    except Exception:
        pass


def downgrade() -> None:
    from sqlalchemy import text
    try:
        op.execute(text("DROP TABLE IF EXISTS futures_backtests"))
    except Exception:
        pass
