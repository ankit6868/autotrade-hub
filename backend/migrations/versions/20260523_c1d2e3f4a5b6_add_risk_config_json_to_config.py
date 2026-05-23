"""add risk_config_json + auto-validate columns

Revision ID: c1d2e3f4a5b6
Revises: b8c9d0e1f2a3
Create Date: 2026-05-23
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
from sqlalchemy import text

revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    try:
        op.execute(text("ALTER TABLE config ADD COLUMN risk_config_json TEXT"))
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.execute(text("ALTER TABLE config DROP COLUMN risk_config_json"))
    except Exception:
        pass
