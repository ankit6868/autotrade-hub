"""add position_mode (hedge support) to strategy_instances

Revision ID: b3c4d5e6f7a8
Revises: e1f2a3b4c5d6
Create Date: 2026-06-06

Adds one column so a bot's position mode survives backend restart:
  • position_mode — "single" (stop-and-reverse, TV default) | "hedge"
                    (a LONG and a SHORT may coexist on the same pair)

Why this matters: hedge mode lives in the running engine (_position_mode).
Without this column, a backend restart would auto-resume a hedge bot in
the safe default "single" mode, silently flipping its risk behaviour back
to stop-and-reverse. Now the DB row carries the user's choice and
auto-resume restores it.

Default "single" matches the engine's default so legacy rows created
before this migration are behaviourally unchanged.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'b3c4d5e6f7a8'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same idempotent pattern as the ARM / strategy-overrides migrations —
    # SQLite (dev) and Postgres (prod via Railway) both accept ADD COLUMN;
    # wrap in try/except so rerunning is a no-op when the column exists.
    for stmt in (
        "ALTER TABLE strategy_instances ADD COLUMN position_mode TEXT DEFAULT 'single'",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass   # column already exists — safe to ignore


def downgrade() -> None:
    # SQLite < 3.35 doesn't support DROP COLUMN; Postgres does. Wrapped in
    # try/except for cross-backend compatibility (same pattern as the ARM
    # downgrade).
    for stmt in (
        "ALTER TABLE strategy_instances DROP COLUMN position_mode",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass
