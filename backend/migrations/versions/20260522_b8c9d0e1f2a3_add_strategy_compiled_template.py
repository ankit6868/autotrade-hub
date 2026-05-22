"""add compiled strategy template + confidence score to strategies

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-05-22

Adds the Phase-5 columns that carry the decoded + resolved StrategyTemplate
JSON plus the live-eligibility metadata (confidence_score, live_permission).
Defaults are 'blocked' / 0 so pre-existing strategies must be re-validated
before live trading can be enabled — which is exactly the safety property
the PDF §3 spec demands.
"""
from __future__ import annotations
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for stmt in (
        "ALTER TABLE strategies ADD COLUMN compiled_template TEXT",
        "ALTER TABLE strategies ADD COLUMN confidence_score INTEGER DEFAULT 0",
        "ALTER TABLE strategies ADD COLUMN live_permission TEXT DEFAULT 'blocked'",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass


def downgrade() -> None:
    for stmt in (
        "ALTER TABLE strategies DROP COLUMN live_permission",
        "ALTER TABLE strategies DROP COLUMN confidence_score",
        "ALTER TABLE strategies DROP COLUMN compiled_template",
    ):
        try:
            op.execute(text(stmt))
        except Exception:
            pass
