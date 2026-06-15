"""Access-code gating for the app.

A single table of redeemable codes. Each code binds to the FIRST account that
redeems it (one code = one Gmail). Two kinds:

  • subscription — expires `duration_days` (30) after activation.
  • unlimited    — never expires (until the admin revokes it).

Admins and the unlimited-email allowlist bypass codes entirely — see
backend/services/access_control.py. This table is auto-created by
Base.metadata.create_all (init_db) and survives via _lightweight_migrate.
"""
from sqlalchemy import Column, Integer, Text, Boolean, DateTime, Index
from sqlalchemy.sql import func
from .database import Base


class AccessCode(Base):
    __tablename__ = "access_codes"

    id            = Column(Integer, primary_key=True)
    code          = Column(Text, nullable=False, unique=True, index=True)
    kind          = Column(Text, nullable=False, default="subscription")  # "subscription" | "unlimited"
    duration_days = Column(Integer, nullable=True)   # 30 for subscription, NULL = lifetime
    # Binding — NULL until the first account redeems it.
    bound_user_id = Column(Text, nullable=True, index=True)   # Clerk user id (stable identity)
    bound_email   = Column(Text, nullable=True)               # the Gmail it was claimed with (display)
    activated_at  = Column(DateTime, nullable=True)           # when first redeemed
    expires_at    = Column(DateTime, nullable=True)           # activated_at + duration; NULL = never
    # Admin kill-switch — revoked codes stop granting access immediately.
    revoked       = Column(Boolean, nullable=False, default=False)
    # When an admin PAUSES a subscription we stamp this; on resume we shift
    # expires_at forward by (now - paused_at) so the timer is FROZEN during the
    # pause and the user continues with the same days remaining. NULL = not paused.
    paused_at     = Column(DateTime, nullable=True)
    note          = Column(Text, nullable=True)               # optional admin label ("June batch", etc.)
    created_at    = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_access_codes_bound_user", "bound_user_id", "revoked"),
    )
