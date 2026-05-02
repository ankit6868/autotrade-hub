"""add webhook_secret, discord_webhook_url, auto_sell columns

Revision ID: b5c2d1e3f4a6
Revises: a3f1c9b2d4e5
Create Date: 2026-05-03 00:00:00.000000
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import inspect, text
import sqlalchemy as sa

revision: str = 'b5c2d1e3f4a6'
down_revision: Union[str, None] = 'a3f1c9b2d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Columns: (name, Column object)
_NEW_COLS = [
    ('auto_sell_enabled',   sa.Column('auto_sell_enabled',   sa.Boolean(), nullable=True, server_default=sa.false())),
    ('auto_sell_mode',      sa.Column('auto_sell_mode',      sa.Text(),    nullable=True, server_default=text("'paper'"))),
    ('discord_webhook_url', sa.Column('discord_webhook_url', sa.Text(),    nullable=True)),
    ('webhook_secret',      sa.Column('webhook_secret',      sa.Text(),    nullable=True)),
]


def upgrade() -> None:
    bind = op.get_bind()
    existing = {c['name'] for c in inspect(bind).get_columns('config')}
    with op.batch_alter_table('config', schema=None) as batch_op:
        for col_name, col_obj in _NEW_COLS:
            if col_name not in existing:
                batch_op.add_column(col_obj)


def downgrade() -> None:
    with op.batch_alter_table('config', schema=None) as batch_op:
        for col_name, _ in reversed(_NEW_COLS):
            try:
                batch_op.drop_column(col_name)
            except Exception:
                pass
