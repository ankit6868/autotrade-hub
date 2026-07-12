"""add regular (normal) KuCoin futures API key columns to config

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
Create Date: 2026-07-10
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for col in ("kucoin_reg_key_enc", "kucoin_reg_secret_enc", "kucoin_reg_passphrase_enc"):
        try:
            op.execute(text(f"ALTER TABLE config ADD COLUMN {col} TEXT"))
        except Exception:
            pass


def downgrade() -> None:
    for col in ("kucoin_reg_key_enc", "kucoin_reg_secret_enc", "kucoin_reg_passphrase_enc"):
        try:
            op.execute(text(f"ALTER TABLE config DROP COLUMN {col}"))
        except Exception:
            pass
