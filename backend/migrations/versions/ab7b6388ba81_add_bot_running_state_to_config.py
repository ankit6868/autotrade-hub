"""add_bot_running_state_to_config

Revision ID: ab7b6388ba81
Revises: b5c2d1e3f4a6
Create Date: 2026-05-06 23:03:23.102204

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'ab7b6388ba81'
down_revision: Union[str, None] = 'b5c2d1e3f4a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("config", sa.Column("bot_running", sa.Boolean(), nullable=True, server_default="false"))
    op.add_column("config", sa.Column("bot_mode", sa.Text(), nullable=True, server_default="paper"))
    op.add_column("config", sa.Column("bot_strategy_name", sa.Text(), nullable=True))
    op.add_column("config", sa.Column("bot_pairs", sa.Text(), nullable=True))
    op.add_column("config", sa.Column("bot_timeframe", sa.Text(), nullable=True, server_default="15m"))
    op.add_column("config", sa.Column("bot_wallet", sa.Float(), nullable=True, server_default="1000.0"))
    op.add_column("config", sa.Column("bot_stoploss", sa.Float(), nullable=True, server_default="-0.03"))


def downgrade() -> None:
    op.drop_column("config", "bot_stoploss")
    op.drop_column("config", "bot_wallet")
    op.drop_column("config", "bot_timeframe")
    op.drop_column("config", "bot_pairs")
    op.drop_column("config", "bot_strategy_name")
    op.drop_column("config", "bot_mode")
    op.drop_column("config", "bot_running")
