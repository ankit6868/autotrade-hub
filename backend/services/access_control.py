"""Access-control logic: admin + unlimited email allowlists, and the
subscription / unlimited code system.

Tiers (checked in this order):
  1. admin                — email in ADMIN_EMAILS → full access + can mint codes.
  2. unlimited (allowlist) — email in UNLIMITED_EMAILS → lifetime access, no code.
  3. redeemed code         — a subscription (30d) or unlimited code bound to them.
  4. none                  — show the code gate.

Local dev (Clerk disabled) is treated as admin so the app isn't gated offline.

Env (set on Railway):
  ADMIN_EMAILS=debasishj.mail30@gmail.com
  UNLIMITED_EMAILS=aknayak24@gmail.com,rushabgurao1@gmail.com,gk5748660@gmail.com
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.access import AccessCode
from backend.utils.clerk_auth import clerk_enabled

# Unambiguous alphabet (no 0/O/1/I/L) for human-typeable codes.
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
DEFAULT_SUBSCRIPTION_DAYS = 30


def _utcnow() -> datetime:
    # Naive UTC throughout, so SQLite (which drops tz) and Postgres compare
    # consistently. Never mix aware/naive datetimes here.
    return datetime.utcnow()


def _csv_env(name: str) -> set[str]:
    raw = os.getenv(name, "") or ""
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def admin_emails() -> set[str]:
    return _csv_env("ADMIN_EMAILS")


def unlimited_emails() -> set[str]:
    return _csv_env("UNLIMITED_EMAILS")


def is_admin(email: str) -> bool:
    # Local dev (no Clerk) → admin, so the app is never gated offline.
    if not clerk_enabled():
        return True
    return bool(email) and email.strip().lower() in admin_emails()


def is_unlimited_allowlisted(email: str) -> bool:
    return bool(email) and email.strip().lower() in unlimited_emails()


# ── code generation ──────────────────────────────────────────────────────
def _random_code() -> str:
    blk = lambda n: "".join(secrets.choice(_ALPHABET) for _ in range(n))
    return f"ATH-{blk(4)}-{blk(4)}"


def generate_codes(db: Session, *, kind: str, count: int,
                   duration_days: int | None = None, note: str | None = None) -> list[str]:
    """Create `count` fresh unclaimed codes. Returns the list of code strings."""
    kind = (kind or "subscription").strip().lower()
    if kind not in ("subscription", "unlimited"):
        raise ValueError("kind must be 'subscription' or 'unlimited'")
    count = max(1, min(500, int(count)))
    if kind == "subscription":
        duration_days = int(duration_days or DEFAULT_SUBSCRIPTION_DAYS)
    else:
        duration_days = None   # unlimited never expires

    out: list[str] = []
    for _ in range(count):
        # Guarantee uniqueness against existing rows.
        for _attempt in range(20):
            code = _random_code()
            exists = db.execute(select(AccessCode.id).where(AccessCode.code == code)).first()
            if not exists:
                break
        db.add(AccessCode(code=code, kind=kind, duration_days=duration_days, note=note))
        out.append(code)
    db.commit()
    return out


# ── status + redeem ──────────────────────────────────────────────────────
def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _best_active_code(db: Session, user_id: str) -> AccessCode | None:
    """The strongest still-valid code bound to this user: an unlimited code
    wins; else the subscription with the latest expiry that's still in the
    future. Returns None if the user has no active code."""
    rows = db.execute(
        select(AccessCode).where(
            AccessCode.bound_user_id == user_id,
            AccessCode.revoked == False,  # noqa: E712
        )
    ).scalars().all()
    now = _utcnow()
    unlimited = [r for r in rows if r.kind == "unlimited"]
    if unlimited:
        return unlimited[0]
    live_subs = [r for r in rows if r.kind == "subscription"
                 and r.expires_at is not None and r.expires_at > now]
    if live_subs:
        return max(live_subs, key=lambda r: r.expires_at)
    return None


def compute_access(db: Session, *, user_id: str, email: str) -> dict:
    """Resolve the caller's access tier. Pure read — never mutates."""
    email = (email or "").strip().lower()
    admin = is_admin(email)
    if admin:
        return {"active": True, "tier": "admin", "is_admin": True,
                "kind": "unlimited", "expires_at": None,
                "email": email, "email_seen": bool(email) or not clerk_enabled()}
    if is_unlimited_allowlisted(email):
        return {"active": True, "tier": "unlimited", "is_admin": False,
                "kind": "unlimited", "expires_at": None,
                "email": email, "email_seen": True}
    code = _best_active_code(db, user_id)
    if code is not None:
        return {"active": True,
                "tier": "unlimited" if code.kind == "unlimited" else "subscription",
                "is_admin": False, "kind": code.kind,
                "expires_at": _iso(code.expires_at),
                "code": code.code, "email": email, "email_seen": bool(email)}
    # Not active — distinguish "had access that ended" (expired or paused) from
    # "never redeemed a code" so the gate can show the right message.
    bound = _codes_for_user(db, user_id)
    if bound:
        paused = any(c.paused_at is not None for c in bound)   # frozen, resumable
        return {"active": False, "tier": "expired", "is_admin": False,
                "kind": None, "expires_at": None, "expired": True,
                "paused": paused, "email": email, "email_seen": bool(email)}
    return {"active": False, "tier": "none", "is_admin": False,
            "kind": None, "expires_at": None, "expired": False,
            "email": email, "email_seen": bool(email)}


def redeem_code(db: Session, *, user_id: str, email: str, code: str) -> dict:
    """Redeem (or renew/upgrade with) a code for this user. One code binds to
    one account forever. Returns {ok, error?, ...status}."""
    code_str = (code or "").strip().upper()
    if not code_str:
        return {"ok": False, "error": "Enter a code."}
    row = db.execute(select(AccessCode).where(AccessCode.code == code_str)).scalar_one_or_none()
    if row is None:
        return {"ok": False, "error": "Invalid code."}
    if row.revoked:
        return {"ok": False, "error": "This code is no longer valid."}

    if row.bound_user_id and row.bound_user_id != user_id:
        # Claimed by someone else → invalid for this account.
        return {"ok": False, "error": "This code is already in use by another account."}

    now = _utcnow()
    if row.bound_user_id == user_id:
        # Already mine. Unlimited or still-valid subscription → fine (idempotent).
        if row.kind == "unlimited" or (row.expires_at and row.expires_at > now):
            return {"ok": True, **compute_access(db, user_id=user_id, email=email)}
        # My own expired subscription code can't self-renew — need a fresh one.
        return {"ok": False, "error": "This code has expired. Enter a new code to continue."}

    # Unclaimed → bind it to this account now.
    row.bound_user_id = user_id
    row.bound_email   = (email or "").strip().lower() or None
    row.activated_at  = now
    row.expires_at    = (now + timedelta(days=int(row.duration_days))
                         if row.kind == "subscription" and row.duration_days else None)
    db.commit()
    return {"ok": True, **compute_access(db, user_id=user_id, email=email)}


def list_codes(db: Session) -> list[dict]:
    """Admin: full inventory for the manage screen."""
    rows = db.execute(select(AccessCode).order_by(AccessCode.created_at.desc())).scalars().all()
    now = _utcnow()
    out = []
    for r in rows:
        live = (not r.revoked) and (
            r.kind == "unlimited" or (r.expires_at and r.expires_at > now)
        ) if r.bound_user_id else (not r.revoked)
        out.append({
            "code": r.code, "kind": r.kind, "duration_days": r.duration_days,
            "bound_email": r.bound_email, "bound": bool(r.bound_user_id),
            "activated_at": _iso(r.activated_at), "expires_at": _iso(r.expires_at),
            "revoked": r.revoked, "note": r.note,
            "status": ("revoked" if r.revoked else
                       "unclaimed" if not r.bound_user_id else
                       "active" if live else "expired"),
        })
    return out


def _codes_for_user(db: Session, user_id: str) -> list[AccessCode]:
    return db.execute(
        select(AccessCode).where(AccessCode.bound_user_id == user_id)
    ).scalars().all()


def list_users(db: Session) -> dict:
    """Admin: everyone who has redeemed a code, grouped by account, with their
    effective access + expiry. Also surfaces the env allowlists (read-only)."""
    rows = db.execute(
        select(AccessCode).where(AccessCode.bound_user_id.isnot(None))
    ).scalars().all()
    now = _utcnow()
    by_user: dict[str, list[AccessCode]] = {}
    for r in rows:
        by_user.setdefault(r.bound_user_id, []).append(r)

    users = []
    for uid, codes in by_user.items():
        unlimited = [c for c in codes if c.kind == "unlimited" and not c.revoked]
        live_subs = [c for c in codes if c.kind == "subscription" and not c.revoked
                     and c.expires_at and c.expires_at > now]
        if unlimited:
            eff, kind, exp, active = unlimited[0], "unlimited", None, True
        elif live_subs:
            eff = max(live_subs, key=lambda c: c.expires_at)
            kind, exp, active = "subscription", _iso(eff.expires_at), True
        else:
            # no active code → show the most recent (expired/revoked) one
            eff = max(codes, key=lambda c: c.activated_at or now)
            kind, exp, active = eff.kind, _iso(eff.expires_at), False
        # most recent email on record for this account
        email = next((c.bound_email for c in sorted(
            codes, key=lambda c: c.activated_at or now, reverse=True) if c.bound_email), None)
        frozen = any(c.paused_at is not None for c in codes)
        users.append({
            "user_id": uid, "email": email, "kind": kind, "active": active,
            "expires_at": exp, "current_code": eff.code,
            "paused": frozen,   # temporarily paused (clock frozen, resumable)
            "revoked": (not active) and (not frozen) and all(c.revoked for c in codes),
            "codes": [{"code": c.code, "kind": c.kind, "revoked": c.revoked,
                       "expires_at": _iso(c.expires_at)} for c in codes],
        })
    users.sort(key=lambda u: (not u["active"], u["email"] or ""))
    allowlist = ([{"email": e, "tier": "admin"} for e in sorted(admin_emails())] +
                 [{"email": e, "tier": "unlimited"} for e in sorted(unlimited_emails())])
    return {"users": users, "allowlist": allowlist}


def extend_user(db: Session, *, user_id: str, add_days: int) -> dict:
    """Admin: add `add_days` to a user's subscription. Extends from the current
    expiry if still active, else from now (reactivates). No-op for unlimited."""
    add_days = int(add_days)
    if add_days <= 0:
        return {"ok": False, "error": "Extension must be a positive number of days."}
    codes = _codes_for_user(db, user_id)
    if not codes:
        return {"ok": False, "error": "User has no redeemed code."}
    if any(c.kind == "unlimited" and not c.revoked for c in codes):
        return {"ok": False, "error": "User already has lifetime (unlimited) access — nothing to extend."}
    now = _utcnow()
    subs = [c for c in codes if c.kind == "subscription"]
    if not subs:
        return {"ok": False, "error": "No subscription code to extend."}
    # Operate on the most generous/recent subscription binding.
    target = max(subs, key=lambda c: c.expires_at or now)
    base = max(target.expires_at or now, now)
    target.expires_at = base + timedelta(days=add_days)
    target.revoked = False   # reactivate if it had lapsed/been revoked
    db.commit()
    return {"ok": True, "user_id": user_id, "code": target.code,
            "expires_at": _iso(target.expires_at)}


def set_user_paused(db: Session, *, user_id: str, paused: bool) -> dict:
    """Admin: pause or resume a user. Pausing gates them immediately AND
    FREEZES the subscription clock; resuming shifts expires_at forward by the
    paused duration, so the user continues with the SAME days remaining (no
    time lost during the pause). Unlimited codes have no clock to freeze."""
    codes = _codes_for_user(db, user_id)
    if not codes:
        return {"ok": False, "error": "User has no redeemed code."}
    now = _utcnow()
    for c in codes:
        if paused:
            c.revoked = True
            # stamp the freeze start once (only for time-based subscriptions)
            if c.kind == "subscription" and c.expires_at and c.paused_at is None:
                c.paused_at = now
        else:
            # resume → push expiry out by however long it was paused
            if c.paused_at is not None and c.expires_at is not None:
                c.expires_at = c.expires_at + (now - c.paused_at)
            c.paused_at = None
            c.revoked = False
    db.commit()
    out = {"ok": True, "user_id": user_id, "paused": bool(paused)}
    if not paused:
        out["status"] = compute_access(db, user_id=user_id, email="")
    return out


def revoke_user(db: Session, *, user_id: str) -> dict:
    """Admin: PERMANENTLY revoke a user — unlike pause, this NULLS the
    subscription (wipes the remaining time). They're cut off immediately and
    must redeem a fresh code to return (extend / change-code can also restore
    them). Use pause/resume for a temporary, time-preserving block instead."""
    codes = _codes_for_user(db, user_id)
    if not codes:
        return {"ok": False, "error": "User has no redeemed code."}
    for c in codes:
        c.revoked = True
        c.expires_at = None   # null the subscription — no time to resume to
        c.paused_at = None
    db.commit()
    return {"ok": True, "user_id": user_id, "revoked": True}


def admin_change_code(db: Session, *, user_id: str, new_code: str | None = None) -> dict:
    """Admin: issue a NEW code to a user (their access string changes), carrying
    over their current expiry/kind, and revoke their old binding. If `new_code`
    is given it must be unclaimed; otherwise a fresh code is generated."""
    codes = _codes_for_user(db, user_id)
    if not codes:
        return {"ok": False, "error": "User has no redeemed code to change."}
    now = _utcnow()
    cur = _best_active_code(db, user_id) or max(codes, key=lambda c: c.activated_at or now)
    kind = cur.kind
    carry_expiry = cur.expires_at if kind == "subscription" else None
    email = cur.bound_email

    if new_code:
        row = db.execute(select(AccessCode).where(
            AccessCode.code == new_code.strip().upper())).scalar_one_or_none()
        if row is None:
            return {"ok": False, "error": "New code not found."}
        if row.revoked:
            return {"ok": False, "error": "New code is revoked."}
        if row.bound_user_id and row.bound_user_id != user_id:
            return {"ok": False, "error": "New code is already in use by another account."}
    else:
        fresh = generate_codes(db, kind=kind, count=1,
                               duration_days=(cur.duration_days if kind == "subscription" else None),
                               note="admin-reissue")
        row = db.execute(select(AccessCode).where(AccessCode.code == fresh[0])).scalar_one()

    # bind the new code to the user, carrying their current access
    row.bound_user_id = user_id
    row.bound_email   = email
    row.activated_at  = now
    row.expires_at    = carry_expiry if kind == "subscription" else None
    row.revoked       = False
    # revoke the user's previous active code(s) so the old string stops working
    for c in codes:
        if c.code != row.code and not c.revoked:
            c.revoked = True
    db.commit()
    return {"ok": True, "user_id": user_id, "new_code": row.code,
            "kind": kind, "expires_at": _iso(row.expires_at)}


def list_recent_signups(db: Session, *, limit: int = 50) -> dict:
    """Admin: recent Clerk sign-ups (email + when), flagged with whether they
    already hold a code. Uses the Clerk Backend API (CLERK_SECRET_KEY)."""
    import json as _json, urllib.request as _ur
    sk = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not sk:
        return {"error": "CLERK_SECRET_KEY not configured — add it in Railway to list sign-ups.",
                "signups": []}
    url = f"https://api.clerk.com/v1/users?limit={max(1, min(100, int(limit)))}&order_by=-created_at"
    try:
        req = _ur.Request(url, headers={"Authorization": f"Bearer {sk}"})
        with _ur.urlopen(req, timeout=15) as r:
            raw = _json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Clerk API error: {e}", "signups": []}
    users = raw if isinstance(raw, list) else (raw.get("data") or [])
    # emails that already have ANY bound code
    bound = {(c.bound_email or "").lower()
             for c in db.execute(select(AccessCode).where(AccessCode.bound_email.isnot(None))).scalars().all()}
    out = []
    for u in users:
        pid = u.get("primary_email_address_id")
        emails = u.get("email_addresses") or []
        email = next((e.get("email_address") for e in emails if e.get("id") == pid), None) \
            or (emails[0].get("email_address") if emails else None)
        email = (email or "").lower()
        out.append({
            "user_id": u.get("id"),
            "email": email,
            "created_at": u.get("created_at"),   # ms epoch
            "has_code": email in bound,
            "is_allowlisted": email in admin_emails() or email in unlimited_emails(),
        })
    return {"signups": out}


def give_code_to_email(db: Session, *, email: str, kind: str = "subscription",
                       duration_days: int | None = None) -> dict:
    """Admin: mint a fresh code for an email and email it via Resend. Returns
    the code regardless of email success so the admin can copy it manually."""
    from backend.services.emailer import send_email, code_email_html
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "error": "A valid email is required."}
    kind = (kind or "subscription").strip().lower()
    if kind not in ("subscription", "unlimited"):
        kind = "subscription"
    code = generate_codes(db, kind=kind, count=1, duration_days=duration_days,
                          note=f"issued to {email}")[0]
    exp_note = (f" — valid {duration_days or DEFAULT_SUBSCRIPTION_DAYS} days from first use"
                if kind == "subscription" else " — lifetime")
    ok, err = send_email(email, "Your AutoTrade Hub access code",
                         code_email_html(code, kind, exp_note))
    return {"ok": True, "code": code, "kind": kind, "emailed": ok,
            "email_error": (None if ok else err)}


def revoke_code(db: Session, *, code: str, revoked: bool = True) -> dict:
    row = db.execute(select(AccessCode).where(AccessCode.code == (code or "").strip().upper())).scalar_one_or_none()
    if row is None:
        return {"ok": False, "error": "Code not found."}
    row.revoked = bool(revoked)
    db.commit()
    return {"ok": True, "code": row.code, "revoked": row.revoked}
