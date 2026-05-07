"""add_strategy_take_profit

Revision ID: d1e2f3a4b5c6
Revises: c8d9e0f1a2b3
Create Date: 2026-05-07

"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    from sqlalchemy import text
    try:
        op.execute(text(
            "ALTER TABLE strategies ADD COLUMN IF NOT EXISTS take_profit FLOAT DEFAULT 0.015"
        ))
    except Exception:
        pass


def downgrade() -> None:
    from sqlalchemy import text
    try:
        op.execute(text("ALTER TABLE strategies DROP COLUMN IF EXISTS take_profit"))
    except Exception:
        pass
