"""add api_mode to strategy_instances + bot_api_mode to config (regular futures bots)

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-07-10
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for tbl, col in (("strategy_instances", "api_mode"), ("config", "bot_api_mode")):
        try:
            op.execute(text(f"ALTER TABLE {tbl} ADD COLUMN {col} TEXT DEFAULT 'lead'"))
        except Exception:
            pass


def downgrade() -> None:
    for tbl, col in (("strategy_instances", "api_mode"), ("config", "bot_api_mode")):
        try:
            op.execute(text(f"ALTER TABLE {tbl} DROP COLUMN {col}"))
        except Exception:
            pass
