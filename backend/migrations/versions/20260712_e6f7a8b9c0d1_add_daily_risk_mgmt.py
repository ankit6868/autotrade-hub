"""add daily risk-management columns to config (max trades/losses per day)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-12
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    stmts = [
        "ALTER TABLE config ADD COLUMN risk_daily_enabled BOOLEAN DEFAULT FALSE",
        "ALTER TABLE config ADD COLUMN risk_max_trades_per_day INTEGER DEFAULT 0",
        "ALTER TABLE config ADD COLUMN risk_max_losses_per_day INTEGER DEFAULT 0",
    ]
    for s in stmts:
        try:
            op.execute(text(s))
        except Exception:
            pass


def downgrade() -> None:
    for col in ("risk_daily_enabled", "risk_max_trades_per_day", "risk_max_losses_per_day"):
        try:
            op.execute(text(f"ALTER TABLE config DROP COLUMN {col}"))
        except Exception:
            pass
