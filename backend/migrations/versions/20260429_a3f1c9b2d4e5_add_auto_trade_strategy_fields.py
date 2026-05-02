"""add auto_trade_strategy_id and auto_trade_pairs to config

Revision ID: a3f1c9b2d4e5
Revises: 11389f98a045
Create Date: 2026-04-29 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3f1c9b2d4e5'
down_revision: Union[str, None] = '11389f98a045'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_trade_strategy_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('auto_trade_pairs', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        batch_op.drop_column('auto_trade_pairs')
        batch_op.drop_column('auto_trade_strategy_id')
