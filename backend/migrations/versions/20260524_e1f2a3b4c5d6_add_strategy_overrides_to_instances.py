"""add per-bot strategy overrides (session / equal-price) to strategy_instances

Revision ID: e1f2a3b4c5d6
Revises: a1b2c3d4e5f6
Create Date: 2026-05-24

Adds three columns so per-bot overrides survive backend restart:
  • session_start_hr_utc — NY/London/Tokyo/24-7 session start hour (0-23)
  • session_end_hr_utc   — session end hour (0-23)
  • equal_price_thresh   — % within which two highs / lows count as "equal"
                           for SMC liquidity-cluster detection

Why this matters: when a user creates an SMCStrategy1 bot with session
"24/7" (start=0, end=23), those settings only lived in the running engine
process. A backend restart would auto-resume the bot but with the safe
default (NY 12-21 UTC), silently changing the bot's behaviour. Now the
DB row carries the user's choice and auto-resume restores it.

Defaults match the bot's pre-restart defaults so legacy rows are
behaviourally unchanged:
  session_start_hr_utc = 12
  session_end_hr_utc   = 21
  equal_price_thresh   = 0.001 (0.1%)
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Same idempotent pattern as the ARM migration — SQLite (dev) and
    # Postgres (prod via Railway) both accept ADD COLUMN; we wrap in
    # try/except so rerunning is a no-op when columns already exist.
    for stmt in (
        "ALTER TABLE strategy_instances ADD COLUMN session_start_hr_utc INTEGER DEFAULT 12",
        "ALTER TABLE strategy_instances ADD COLUMN session_end_hr_utc INTEGER DEFAULT 21",
        "ALTER TABLE strategy_instances ADD COLUMN equal_price_thresh FLOAT DEFAULT 0.001",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass   # column already exists — safe to ignore


def downgrade() -> None:
    # SQLite < 3.35 doesn't support DROP COLUMN; Postgres does. Wrapped
    # in try/except for cross-backend compatibility (same pattern as the
    # ARM downgrade).
    for stmt in (
        "ALTER TABLE strategy_instances DROP COLUMN equal_price_thresh",
        "ALTER TABLE strategy_instances DROP COLUMN session_end_hr_utc",
        "ALTER TABLE strategy_instances DROP COLUMN session_start_hr_utc",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass
