"""Minimal transactional email sender (no extra dependency — pure urllib).

Supports two providers, picked automatically by which env vars are set:

  Brevo (recommended — free 300/day, no domain needed, just verify a sender):
    BREVO_API_KEY    — your Brevo API key (xkeysib-…)
    BREVO_FROM       — your VERIFIED sender, e.g. "AutoTrade Hub <you@gmail.com>"
                       (must be a sender you've confirmed in Brevo)

  Resend (only sends to arbitrary recipients once you verify a domain):
    RESEND_API_KEY   — your Resend API key (re_…)
    RESEND_FROM      — verified sender, e.g. "AutoTrade Hub <noreply@yourdomain>"

If BREVO_API_KEY is set it is used; else RESEND_API_KEY; else email is off.
send_email() returns (ok, error) and NEVER raises — callers degrade gracefully
(e.g. still return the generated code so the admin can copy it manually).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

_DEFAULT_RESEND_FROM = "AutoTrade Hub <onboarding@resend.dev>"


def email_configured() -> bool:
    return bool(os.getenv("BREVO_API_KEY", "").strip()
                or os.getenv("RESEND_API_KEY", "").strip())


def _parse_from(raw: str, default_name: str = "AutoTrade Hub") -> tuple[str, str]:
    """'Name <email@x>' -> ('Name', 'email@x'); 'email@x' -> (default, 'email@x')."""
    raw = (raw or "").strip()
    if "<" in raw and ">" in raw:
        name = raw.split("<", 1)[0].strip().strip('"') or default_name
        email = raw.split("<", 1)[1].split(">", 1)[0].strip()
        return name, email
    return default_name, raw


def _post_json(url: str, headers: dict, payload: dict) -> tuple[bool, str, dict]:
    """POST JSON; return (ok, error, parsed_body). Reads HTTPError body so the
    provider's real message (not a bare 'Forbidden') reaches the caller."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return True, "", data
    except urllib.error.HTTPError as he:
        try:
            err = json.loads(he.read().decode())
            return False, (err.get("message") or err.get("error") or f"HTTP {he.code}: {err}"), {}
        except Exception:
            return False, f"HTTP {he.code} {he.reason}", {}
    except Exception as e:  # pragma: no cover — network/transport
        return False, str(e), {}


def _send_brevo(to: str, subject: str, html: str) -> tuple[bool, str]:
    api_key = os.getenv("BREVO_API_KEY", "").strip()
    name, email = _parse_from(os.getenv("BREVO_FROM", ""))
    if not email:
        return False, "BREVO_FROM not set — put your verified sender, e.g. 'AutoTrade Hub <you@gmail.com>'"
    ok, err, data = _post_json(
        "https://api.brevo.com/v3/smtp/email",
        {"api-key": api_key, "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": "AutoTradeHub/1.0"},
        {"sender": {"name": name, "email": email},
         "to": [{"email": to}], "subject": subject, "htmlContent": html},
    )
    if not ok:
        return False, err
    if data.get("messageId"):
        return True, ""
    return False, str(data)


def _send_resend(to: str, subject: str, html: str) -> tuple[bool, str]:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    frm = os.getenv("RESEND_FROM", "").strip() or _DEFAULT_RESEND_FROM
    ok, err, data = _post_json(
        "https://api.resend.com/emails",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
         "Accept": "application/json", "User-Agent": "AutoTradeHub/1.0"},
        {"from": frm, "to": [to], "subject": subject, "html": html},
    )
    if not ok:
        return False, err
    if data.get("id"):
        return True, ""
    return False, str(data)


def send_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    if os.getenv("BREVO_API_KEY", "").strip():
        return _send_brevo(to, subject, html)
    if os.getenv("RESEND_API_KEY", "").strip():
        return _send_resend(to, subject, html)
    return False, "No email provider configured (set BREVO_API_KEY or RESEND_API_KEY)"


def code_email_html(code: str, kind: str, expires_note: str) -> str:
    """Branded HTML for the access-code email."""
    return f"""
    <div style="font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:520px;margin:0 auto;padding:24px;color:#0f172a">
      <h2 style="margin:0 0 8px">Your AutoTrade Hub access code</h2>
      <p style="color:#475569;margin:0 0 18px">Thanks for joining. Use the code below to unlock the app.</p>
      <div style="background:#0f1729;color:#e2e8f0;border-radius:12px;padding:18px;text-align:center;font-size:22px;letter-spacing:3px;font-weight:700">
        {code}
      </div>
      <p style="color:#475569;margin:16px 0 0">Plan: <b>{kind}</b>{expires_note}.</p>
      <ol style="color:#475569;font-size:14px;line-height:1.6">
        <li>Log in to AutoTrade Hub with this email.</li>
        <li>On the access screen, paste the code above and click Unlock.</li>
      </ol>
      <p style="color:#94a3b8;font-size:12px;margin-top:18px">This code is tied to your account on first use and can't be shared.</p>
    </div>
    """
