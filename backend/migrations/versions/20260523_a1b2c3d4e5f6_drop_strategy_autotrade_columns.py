"""drop orphan auto_trade / copy_trading columns from strategies

Revision ID: a1b2c3d4e5f6
Revises: c1d2e3f4a5b6
Create Date: 2026-05-23

The autotrade_engine + copy_trading services were deleted in the spot
purge. The columns they wrote on Strategy (auto_trade_enabled,
auto_trade_mode, allow_copy_trading) are now orphaned — nothing reads
them. Drop them so the schema reflects the futures-only build.

Best-effort: SQLite < 3.35 doesn't support DROP COLUMN. The migration
swallows that error so dev databases on older SQLite still upgrade.
Production runs on Postgres which supports DROP COLUMN cleanly.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for stmt in (
        "ALTER TABLE strategies DROP COLUMN allow_copy_trading",
        "ALTER TABLE strategies DROP COLUMN auto_trade_mode",
        "ALTER TABLE strategies DROP COLUMN auto_trade_enabled",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass


def downgrade() -> None:
    for stmt in (
        "ALTER TABLE strategies ADD COLUMN auto_trade_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE strategies ADD COLUMN auto_trade_mode TEXT DEFAULT 'paper'",
        "ALTER TABLE strategies ADD COLUMN allow_copy_trading BOOLEAN DEFAULT 0",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass
