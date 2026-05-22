"""add ARM (Advanced Risk Management) columns to strategy_instances

Revision ID: a7b8c9d0e1f2
Revises: f3a4b5c6d7e8
Create Date: 2026-05-22

Adds the five ARM config columns so a bot's TP1/TP2 + BE-trail settings
survive backend restart (auto-resume reads them in GET /api/futures/bots).

All columns have safe defaults that match "ARM off" — existing rows
created before this migration keep their legacy single-TP behaviour.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite (dev) needs ADD COLUMN, but Postgres (prod via Railway) also
    # accepts the same syntax. IF NOT EXISTS is Postgres-only — SQLite will
    # raise if the column already exists, so we wrap in try/except per the
    # existing migration pattern in this repo.
    for stmt in (
        "ALTER TABLE strategy_instances ADD COLUMN arm_enabled BOOLEAN DEFAULT 0",
        "ALTER TABLE strategy_instances ADD COLUMN arm_tp1_close_pct FLOAT DEFAULT 50.0",
        "ALTER TABLE strategy_instances ADD COLUMN arm_be_mode TEXT DEFAULT 'leverage'",
        "ALTER TABLE strategy_instances ADD COLUMN arm_be_buffer_pct FLOAT DEFAULT 1.0",
        "ALTER TABLE strategy_instances ADD COLUMN arm_trail_to_tp1 BOOLEAN DEFAULT 1",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass   # column already exists — safe to ignore


def downgrade() -> None:
    # SQLite < 3.35 doesn't support DROP COLUMN; Postgres does. We try
    # each drop in try/except so the downgrade is best-effort across both
    # backends — matching the pattern in the strategy_take_profit migration.
    for stmt in (
        "ALTER TABLE strategy_instances DROP COLUMN arm_trail_to_tp1",
        "ALTER TABLE strategy_instances DROP COLUMN arm_be_buffer_pct",
        "ALTER TABLE strategy_instances DROP COLUMN arm_be_mode",
        "ALTER TABLE strategy_instances DROP COLUMN arm_tp1_close_pct",
        "ALTER TABLE strategy_instances DROP COLUMN arm_enabled",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass
