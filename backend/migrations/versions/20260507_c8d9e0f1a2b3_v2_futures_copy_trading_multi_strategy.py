"""v2_futures_copy_trading_multi_strategy

Adds columns and tables for:
  - Futures trading (leverage, liquidation_price, market_type on trades)
  - Copy trading (copy_signals, copy_subscriptions tables)
  - Multi-strategy (strategy_instances table)
  - Copy trading opt-in on strategies (allow_copy_trading, default_leverage)

Revision ID: c8d9e0f1a2b3
Revises: ab7b6388ba81
Create Date: 2026-05-07 06:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'ab7b6388ba81'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── trades: add futures columns ───────────────────────────────────────
    op.add_column("trades", sa.Column(
        "market_type", sa.Text(), nullable=True, server_default="spot"
    ))
    op.add_column("trades", sa.Column(
        "leverage", sa.Integer(), nullable=True, server_default="1"
    ))
    op.add_column("trades", sa.Column(
        "liquidation_price", sa.Float(), nullable=True
    ))
    op.add_column("trades", sa.Column(
        "copy_source_id", sa.Integer(), nullable=True
    ))

    # ── strategies: add copy trading columns ─────────────────────────────
    op.add_column("strategies", sa.Column(
        "allow_copy_trading", sa.Boolean(), nullable=True, server_default="false"
    ))
    op.add_column("strategies", sa.Column(
        "default_leverage", sa.Integer(), nullable=True, server_default="1"
    ))

    # ── strategy_instances: multi-strategy per user ───────────────────────
    op.create_table(
        "strategy_instances",
        sa.Column("id",            sa.Integer(), primary_key=True),
        sa.Column("user_id",       sa.Text(),    nullable=False, index=True),
        sa.Column("strategy_id",   sa.Integer(), sa.ForeignKey("strategies.id"), nullable=True),
        sa.Column("strategy_name", sa.Text(),    nullable=False),
        sa.Column("market_type",   sa.Text(),    nullable=True, server_default="spot"),
        sa.Column("mode",          sa.Text(),    nullable=True, server_default="paper"),
        sa.Column("pairs",         sa.Text(),    nullable=True, server_default="BTC/USDT"),
        sa.Column("leverage",      sa.Integer(), nullable=True, server_default="1"),
        sa.Column("timeframe",     sa.Text(),    nullable=True, server_default="15m"),
        sa.Column("stoploss",      sa.Float(),   nullable=True, server_default="-0.03"),
        sa.Column("takeprofit",    sa.Float(),   nullable=True, server_default="0.015"),
        sa.Column("wallet",        sa.Float(),   nullable=True, server_default="1000.0"),
        sa.Column("risk_pct",      sa.Float(),   nullable=True, server_default="5.0"),
        sa.Column("is_running",    sa.Boolean(), nullable=True, server_default="false"),
        sa.Column("engine_key",    sa.Text(),    nullable=True, unique=True),
        sa.Column("total_trades",  sa.Integer(), nullable=True, server_default="0"),
        sa.Column("total_pnl",     sa.Float(),   nullable=True, server_default="0.0"),
        sa.Column("created_at",    sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at",    sa.DateTime(), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index("ix_strategy_instances_user_id", "strategy_instances", ["user_id"])

    # ── copy_signals: master broadcast records ────────────────────────────
    op.create_table(
        "copy_signals",
        sa.Column("id",              sa.Integer(), primary_key=True),
        sa.Column("master_user_id",  sa.Text(),    nullable=False, index=True),
        sa.Column("pair",            sa.Text(),    nullable=False),
        sa.Column("direction",       sa.Text(),    nullable=True, server_default="long"),
        sa.Column("market_type",     sa.Text(),    nullable=True, server_default="spot"),
        sa.Column("leverage",        sa.Integer(), nullable=True, server_default="1"),
        sa.Column("entry_price",     sa.Float(),   nullable=True),
        sa.Column("sl_price",        sa.Float(),   nullable=True),
        sa.Column("tp_price",        sa.Float(),   nullable=True),
        sa.Column("stake_pct",       sa.Float(),   nullable=True, server_default="5.0"),
        sa.Column("strategy_name",   sa.Text(),    nullable=True),
        sa.Column("signal_type",     sa.Text(),    nullable=True, server_default="entry"),
        sa.Column("profit_pct",      sa.Float(),   nullable=True),
        sa.Column("profit_abs",      sa.Float(),   nullable=True),
        sa.Column("broadcasted_at",  sa.DateTime(), server_default=sa.func.now()),
        sa.Column("expires_at",      sa.DateTime(), nullable=True),
        sa.Column("closed_at",       sa.DateTime(), nullable=True),
    )
    op.create_index("ix_copy_signals_master_user_id", "copy_signals", ["master_user_id"])

    # ── copy_subscriptions: follower → master relationships ───────────────
    op.create_table(
        "copy_subscriptions",
        sa.Column("id",                  sa.Integer(), primary_key=True),
        sa.Column("follower_user_id",    sa.Text(),    nullable=False, index=True),
        sa.Column("master_user_id",      sa.Text(),    nullable=False, index=True),
        sa.Column("is_active",           sa.Boolean(), nullable=True, server_default="true"),
        sa.Column("copy_mode",           sa.Text(),    nullable=True, server_default="paper"),
        sa.Column("copy_market_type",    sa.Text(),    nullable=True, server_default="spot"),
        sa.Column("max_leverage",        sa.Integer(), nullable=True, server_default="10"),
        sa.Column("stake_override_pct",  sa.Float(),   nullable=True),
        sa.Column("total_copied",        sa.Integer(), nullable=True, server_default="0"),
        sa.Column("total_profit",        sa.Float(),   nullable=True, server_default="0.0"),
        sa.Column("win_count",           sa.Integer(), nullable=True, server_default="0"),
        sa.Column("created_at",          sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_copy_subscriptions_follower", "copy_subscriptions", ["follower_user_id"])
    op.create_index("ix_copy_subscriptions_master",   "copy_subscriptions", ["master_user_id"])


def downgrade() -> None:
    op.drop_table("copy_subscriptions")
    op.drop_table("copy_signals")
    op.drop_table("strategy_instances")
    op.drop_column("strategies", "default_leverage")
    op.drop_column("strategies", "allow_copy_trading")
    op.drop_column("trades", "copy_source_id")
    op.drop_column("trades", "liquidation_price")
    op.drop_column("trades", "leverage")
    op.drop_column("trades", "market_type")
