"""Minimal transactional email sender (Resend).

Sends via Resend's HTTP API using urllib (no extra dependency). Configure:
  RESEND_API_KEY   — your Resend API key (required to actually send)
  RESEND_FROM      — verified sender, e.g. "AutoTrade Hub <noreply@yourdomain>"
                     (defaults to Resend's test address, which only delivers to
                     your own account email until you verify a real domain)

send_email() returns (ok, error) and NEVER raises — callers degrade gracefully
(e.g. still return the generated code so the admin can copy it manually).
"""
from __future__ import annotations

import json
import os
import urllib.request

_DEFAULT_FROM = "AutoTrade Hub <onboarding@resend.dev>"


def email_configured() -> bool:
    return bool(os.getenv("RESEND_API_KEY", "").strip())


def send_email(to: str, subject: str, html: str) -> tuple[bool, str]:
    api_key = os.getenv("RESEND_API_KEY", "").strip()
    if not api_key:
        return False, "RESEND_API_KEY not configured"
    frm = os.getenv("RESEND_FROM", "").strip() or _DEFAULT_FROM
    body = json.dumps({"from": frm, "to": [to], "subject": subject, "html": html}).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=body, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("id"):
            return True, ""
        return False, data.get("message") or str(data)
    except Exception as e:  # pragma: no cover — network/transport
        return False, str(e)


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
