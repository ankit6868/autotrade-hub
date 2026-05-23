"""
NICE-7 — Discord notifier.

Posts engine events (position open/close, liquidation, daily-DD trip,
strategy compile failed, bot paused/resumed) to the user's configured
Discord webhook URL. Stored on Config.discord_webhook_url (already a
column, no migration needed).

Best-effort: send failures are logged at DEBUG but never bubble back
into the trading loop — a misconfigured webhook can NEVER block a trade.

Two-tier interface:
  • notify(user_id, title, description, level)   — sync, blocks on httpx
  • notify_async(...)                            — fire-and-forget thread

The engine should always use notify_async to avoid adding KuCoin-call
latency to the signal-scan loop.

Event-shape conventions (so Discord embeds look consistent):
  • level='success' → emerald 0x22c55e   — position opened / TP hit
  • level='info'    → blue    0x3b82f6   — paused / resumed / regular close
  • level='warn'    → amber   0xf59e0b   — cooldown skip / spread block / wind-down
  • level='danger'  → red     0xef4444   — liquidation / daily DD trip / compile failure
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("notifier")


# Map level → Discord embed colour (integer RGB).
_COLORS = {
    "success": 0x22c55e,
    "info":    0x3b82f6,
    "warn":    0xf59e0b,
    "danger":  0xef4444,
}


def _webhook_url_for(user_id: str) -> Optional[str]:
    """Fetch the user's Discord webhook URL from Config. None = no notifier."""
    try:
        from backend.models import SessionLocal, Config
        from sqlalchemy import select
        with SessionLocal() as db:
            cfg = db.execute(
                select(Config).where(Config.user_id == user_id).limit(1)
            ).scalar_one_or_none()
        if cfg and getattr(cfg, "discord_webhook_url", None):
            url = cfg.discord_webhook_url.strip()
            # Defensive: only accept Discord webhook URLs to prevent SSRF.
            if url.startswith("https://discord.com/api/webhooks/") or \
               url.startswith("https://discordapp.com/api/webhooks/"):
                return url
            log.debug("notifier: %s discord_webhook_url not a Discord URL — skipping", user_id)
    except Exception as e:
        log.debug("notifier: lookup failed for %s: %s", user_id, e)
    return None


def notify(
    user_id: str,
    *,
    title:       str,
    description: str = "",
    level:       str = "info",
    fields:      Optional[list[dict]] = None,
) -> bool:
    """Synchronously POST a Discord embed. Returns True on HTTP 2xx."""
    url = _webhook_url_for(user_id)
    if not url:
        return False
    embed = {
        "title":       title[:256],         # Discord embed limit
        "description": description[:4096],  # Discord embed limit
        "color":       _COLORS.get(level, _COLORS["info"]),
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "footer":      {"text": "AutoTrade Hub"},
    }
    if fields:
        # Each field {name, value, inline?} per Discord schema.
        clean_fields: list[dict] = []
        for f in fields[:25]:
            clean_fields.append({
                "name":   str(f.get("name", ""))[:256],
                "value":  str(f.get("value", ""))[:1024],
                "inline": bool(f.get("inline", True)),
            })
        embed["fields"] = clean_fields
    payload = json.dumps({"embeds": [embed]}).encode()
    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        log.debug("notifier: send for %s failed: %s", user_id, e)
        return False


def notify_async(user_id: str, **kwargs) -> None:
    """Fire-and-forget notification — never blocks the caller.

    The trading loop calls this on every position open/close/etc., so it
    MUST NOT add KuCoin-call-level latency. Worst case is a thread that
    dies silently; the trade itself is unaffected.
    """
    threading.Thread(
        target=lambda: notify(user_id, **kwargs),
        daemon=True,
        name=f"notifier-{user_id}",
    ).start()


# ── Convenience builders for the most common engine events ──────────────

def notify_position_opened(user_id: str, *, pair: str, direction: str,
                            entry: float, sl: float, tp: float, leverage: int,
                            strategy: str, mode: str, arm: bool = False,
                            tp1: Optional[float] = None, tp2: Optional[float] = None) -> None:
    fields = [
        {"name": "Pair",      "value": pair,                          "inline": True},
        {"name": "Direction", "value": direction.upper(),             "inline": True},
        {"name": "Mode",      "value": mode.upper(),                  "inline": True},
        {"name": "Entry",     "value": f"{entry:.4f}",                "inline": True},
        {"name": "SL",        "value": f"{sl:.4f}",                   "inline": True},
        {"name": "TP",        "value": f"{tp:.4f}",                   "inline": True},
        {"name": "Leverage",  "value": f"{leverage}x",                "inline": True},
        {"name": "Strategy",  "value": strategy,                      "inline": True},
    ]
    if arm and tp1 and tp2:
        fields.append({"name": "ARM", "value": f"TP1 {tp1:.4f} → TP2 {tp2:.4f}", "inline": False})
    notify_async(user_id, title=f"📈 Opened {direction.upper()} {pair}", level="success", fields=fields)


def notify_position_closed(user_id: str, *, pair: str, direction: str,
                            entry: float, exit_p: float, pnl: float,
                            reason: str, leverage: int, mode: str) -> None:
    sign  = "+" if pnl >= 0 else ""
    level = "success" if pnl >= 0 else "warn"
    fields = [
        {"name": "Pair",     "value": pair,                "inline": True},
        {"name": "Direction","value": direction.upper(),   "inline": True},
        {"name": "Mode",     "value": mode.upper(),        "inline": True},
        {"name": "Entry",    "value": f"{entry:.4f}",      "inline": True},
        {"name": "Exit",     "value": f"{exit_p:.4f}",     "inline": True},
        {"name": "P&L",      "value": f"{sign}{pnl:.2f} USDT", "inline": True},
        {"name": "Reason",   "value": reason,              "inline": True},
        {"name": "Leverage", "value": f"{leverage}x",      "inline": True},
    ]
    icon = "✅" if pnl >= 0 else "🔻"
    notify_async(user_id, title=f"{icon} Closed {pair} ({reason})", level=level, fields=fields)


def notify_liquidated(user_id: str, *, pair: str, direction: str,
                       liq_price: float, pnl: float, leverage: int, mode: str) -> None:
    notify_async(
        user_id,
        title=f"💀 LIQUIDATED {pair} {direction.upper()}",
        description=f"Mode: **{mode.upper()}** · Leverage: **{leverage}x**",
        level="danger",
        fields=[
            {"name": "Liquidation Price", "value": f"{liq_price:.4f}",    "inline": True},
            {"name": "P&L",               "value": f"{pnl:.2f} USDT",     "inline": True},
        ],
    )


def notify_daily_dd_trip(user_id: str, *, pnl_pct: float, limit_pct: float,
                          starting_balance: float, balance_now: float) -> None:
    notify_async(
        user_id,
        title="🛑 Daily Drawdown Tripped",
        description=(
            f"Today's P&L dropped to **{pnl_pct:.2f}%** of starting balance, "
            f"crossing the **−{limit_pct}%** limit. No new entries until "
            "UTC midnight."
        ),
        level="danger",
        fields=[
            {"name": "Starting balance", "value": f"{starting_balance:.2f} USDT", "inline": True},
            {"name": "Current balance",  "value": f"{balance_now:.2f} USDT",     "inline": True},
        ],
    )


def notify_compile_failed(user_id: str, *, strategy: str, error: str) -> None:
    notify_async(
        user_id,
        title=f"⚠ Strategy compile failed: {strategy}",
        description=f"```\n{error[:1500]}\n```\nThe bot is auto-stopped — edit the strategy code or re-upload.",
        level="danger",
    )


def notify_paused(user_id: str, *, strategy: str, mode: str) -> None:
    notify_async(
        user_id,
        title=f"⏸ Bot paused: {strategy}",
        description=f"Mode: **{mode.upper()}**. Open positions keep being managed; no new entries.",
        level="info",
    )


def notify_resumed(user_id: str, *, strategy: str, mode: str) -> None:
    notify_async(
        user_id,
        title=f"▶ Bot resumed: {strategy}",
        description=f"Mode: **{mode.upper()}**. New entries re-enabled.",
        level="info",
    )
