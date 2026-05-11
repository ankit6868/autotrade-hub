"""add_futures_orders_table

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-05-11

"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'futures_orders',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Text(), nullable=False),
        sa.Column('symbol', sa.Text(), nullable=False),
        sa.Column('side', sa.Text(), nullable=False),
        sa.Column('order_type', sa.Text(), nullable=False, server_default='limit'),
        sa.Column('size', sa.Float(), nullable=False),
        sa.Column('price', sa.Float(), nullable=True),
        sa.Column('stop_price', sa.Float(), nullable=True),
        sa.Column('leverage', sa.Integer(), server_default='1'),
        sa.Column('margin_mode', sa.Text(), server_default='cross'),
        sa.Column('client_oid', sa.Text(), nullable=True),
        sa.Column('exchange_order_id', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), server_default='pending'),
        sa.Column('time_in_force', sa.Text(), server_default='GTC'),
        sa.Column('hidden', sa.Boolean(), server_default='0'),
        sa.Column('post_only', sa.Boolean(), server_default='0'),
        sa.Column('reduce_only', sa.Boolean(), server_default='0'),
        sa.Column('close_order', sa.Boolean(), server_default='0'),
        sa.Column('tp_price', sa.Float(), nullable=True),
        sa.Column('sl_price', sa.Float(), nullable=True),
        sa.Column('filled_size', sa.Float(), server_default='0'),
        sa.Column('filled_price', sa.Float(), nullable=True),
        sa.Column('fee', sa.Float(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('filled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.CheckConstraint("side IN ('buy', 'sell')"),
        sa.CheckConstraint("order_type IN ('limit', 'market', 'stop', 'stop_limit', 'trailing_stop', 'twap')"),
        sa.CheckConstraint("status IN ('pending', 'active', 'filled', 'partially_filled', 'cancelled', 'triggered')"),
        sa.CheckConstraint("margin_mode IN ('cross', 'isolated')"),
    )
    op.create_index('ix_futures_orders_user_id', 'futures_orders', ['user_id'])
    op.create_index('ix_futures_orders_user_status', 'futures_orders', ['user_id', 'status'])


def downgrade() -> None:
    op.drop_index('ix_futures_orders_user_status', 'futures_orders')
    op.drop_index('ix_futures_orders_user_id', 'futures_orders')
    op.drop_table('futures_orders')
