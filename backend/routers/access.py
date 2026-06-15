"""Access-code gate endpoints + guard dependencies.

  GET  /api/access/status          → caller's access tier (used by the gate)
  POST /api/access/redeem          → redeem / renew / upgrade with a code
  GET  /api/access/codes           → (admin) list all codes
  POST /api/access/codes           → (admin) generate N codes
  POST /api/access/codes/revoke    → (admin) revoke / un-revoke a code

Guards exported for other routers:
  require_active_access  — 402 unless the caller is admin / unlimited / has an
                           active code. Applied to the futures router so the
                           gate can't be bypassed by hitting the API directly.
  require_admin          — 403 unless the caller's email is in ADMIN_EMAILS.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.utils.clerk_auth import get_user_id
from backend.services import access_control as AC

log = logging.getLogger("access")
router = APIRouter(prefix="/api/access", tags=["access"])


def _email_for(request: Request, x_user_email: str | None) -> str:
    """Email from the JWT claim (stashed by get_user_id) else the
    X-User-Email header fallback. Lowercased."""
    email = (getattr(request.state, "user_email", "") or "").strip().lower()
    if not email and x_user_email and "@" in x_user_email:
        email = x_user_email.strip().lower()
        request.state.user_email = email
    return email


# ── guard dependencies (used by other routers) ──────────────────────────
def require_active_access(
    request: Request,
    user_id: str = Depends(get_user_id),
    x_user_email: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    """Block the request (402) unless the caller has access. Fails OPEN on an
    unexpected error (DB hiccup) so an infra glitch can't brick the whole
    trading app — the frontend gate remains the primary barrier."""
    try:
        email = _email_for(request, x_user_email)
        st = AC.compute_access(db, user_id=user_id, email=email)
    except Exception as e:  # pragma: no cover — defensive
        log.warning("access check failed open for %s: %s", user_id, e)
        return {"active": True, "tier": "error_fail_open"}
    if not st.get("active"):
        raise HTTPException(status_code=402, detail="ACCESS_CODE_REQUIRED")
    return st


def require_admin(
    request: Request,
    user_id: str = Depends(get_user_id),
    x_user_email: str | None = Header(default=None),
):
    email = _email_for(request, x_user_email)
    if not AC.is_admin(email):
        raise HTTPException(status_code=403, detail="Admin access required.")
    return email


# ── endpoints ────────────────────────────────────────────────────────────
@router.get("/status")
def access_status(
    request: Request,
    user_id: str = Depends(get_user_id),
    x_user_email: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email = _email_for(request, x_user_email)
    return AC.compute_access(db, user_id=user_id, email=email)


@router.post("/redeem")
def access_redeem(
    req: dict,
    request: Request,
    user_id: str = Depends(get_user_id),
    x_user_email: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    email = _email_for(request, x_user_email)
    return AC.redeem_code(db, user_id=user_id, email=email, code=str(req.get("code", "")))


@router.get("/codes")
def admin_list_codes(db: Session = Depends(get_db), _admin: str = Depends(require_admin)):
    return {"codes": AC.list_codes(db)}


@router.post("/codes")
def admin_generate_codes(
    req: dict,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    kind  = str(req.get("kind", "subscription"))
    count = int(req.get("count", 1))
    days  = req.get("duration_days")
    note  = req.get("note")
    try:
        codes = AC.generate_codes(db, kind=kind, count=count,
                                  duration_days=days, note=note)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "kind": kind, "count": len(codes), "codes": codes}


@router.post("/codes/revoke")
def admin_revoke_code(
    req: dict,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    return AC.revoke_code(db, code=str(req.get("code", "")),
                          revoked=bool(req.get("revoked", True)))


# ── Admin: user management ───────────────────────────────────────────────
@router.get("/users")
def admin_list_users(db: Session = Depends(get_db), _admin: str = Depends(require_admin)):
    """Every code-redeeming account + their expiry, plus the env allowlists."""
    return AC.list_users(db)


@router.post("/users/extend")
def admin_extend_user(
    req: dict,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Add time to a user's subscription. Accepts `days` and/or `months`
    (1 month = 30 days). Identify the user by `user_id`."""
    user_id = str(req.get("user_id", "")).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    add_days = int(req.get("days", 0) or 0) + int(req.get("months", 0) or 0) * 30
    return AC.extend_user(db, user_id=user_id, add_days=add_days)


@router.post("/users/change-code")
def admin_change_user_code(
    req: dict,
    db: Session = Depends(get_db),
    _admin: str = Depends(require_admin),
):
    """Issue a new code to a user (carrying their current expiry/kind) and
    revoke their old one. Optional `new_code` (must be unclaimed); otherwise a
    fresh code is generated and returned."""
    user_id = str(req.get("user_id", "")).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    new_code = req.get("new_code")
    return AC.admin_change_code(db, user_id=user_id,
                                new_code=str(new_code) if new_code else None)
