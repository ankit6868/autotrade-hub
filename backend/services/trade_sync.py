"""Sync trades from Freqtrade's per-user SQLite DBs into our `trades` table.

Each user owns a directory at `<FREQTRADE_USERDIR>/<safe_user_id>/` containing
`tradesv3.sqlite` (live) and `tradesv3.dryrun.sqlite` (paper). We read those
files directly via raw SQL so we don't have to import freqtrade just for the
schema. Every row inserted into our table is tagged with `user_id` so the
multi-tenant queries in the routers stay isolated.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.trade import Trade
from backend.models import Strategy
from backend.services.freqtrade_manager import _safe_subdir

FREQTRADE_USERDIR_ROOT = os.getenv("FREQTRADE_USERDIR", "./user_data")


def _user_db_paths(user_id: str) -> tuple[Path, Path]:
    base = Path(FREQTRADE_USERDIR_ROOT) / _safe_subdir(user_id)
    return base / "tradesv3.sqlite", base / "tradesv3.dryrun.sqlite"


def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.utcfromtimestamp(value)
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except Exception:
        return None


def _read_freqtrade_trades(db_path: Path, mode: str) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT id, pair, is_open, amount, open_rate, close_rate,
                   close_profit, close_profit_abs, stop_loss,
                   open_date, close_date, exit_reason, strategy
            FROM trades
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()

    out: list[dict] = []
    for r in rows:
        out.append(
            {
                "ft_id": r["id"],
                "pair": r["pair"],
                "mode": mode,
                "side": "long",
                "entry_price": r["open_rate"],
                "exit_price": r["close_rate"],
                "amount": r["amount"],
                "profit_pct": (r["close_profit"] or 0) * 100 if r["close_profit"] is not None else None,
                "profit_abs": r["close_profit_abs"],
                "stoploss_price": r["stop_loss"],
                "entry_time": _parse_dt(r["open_date"]),
                "exit_time": _parse_dt(r["close_date"]),
                "exit_reason": r["exit_reason"],
                "status": "open" if r["is_open"] else "closed",
                "strategy_name": r["strategy"],  # class name string from freqtrade
            }
        )
    return out


def _resolve_strategy_id(db: Session, user_id: str, strategy_name: str | None) -> int | None:
    """Look up the Strategy row ID by matching the class name in generated_code."""
    if not strategy_name:
        return None
    rows = db.execute(
        select(Strategy)
        .where(
            Strategy.user_id == user_id,
            Strategy.generated_code.like(f"%class {strategy_name}(%"),
        )
        .order_by(Strategy.created_at.desc())
    ).scalars().all()
    return rows[0].id if rows else None


def sync(db: Session, user_id: str) -> dict:
    """Pull trades from a single user's freqtrade DBs into our trades table.

    Existing rows are matched on (user_id, mode, pair, entry_time). The
    user_id scope ensures one user's bot can never overwrite another user's
    trades, even if they happen to be on the same pair at the same minute.
    """
    live_db, dry_db = _user_db_paths(user_id)
    sources = [(dry_db, "paper"), (live_db, "live")]
    inserted = 0
    updated = 0
    total = 0

    # Cache strategy name → id lookups to avoid redundant DB queries
    _strategy_id_cache: dict[str, int | None] = {}

    for db_path, mode in sources:
        rows = _read_freqtrade_trades(db_path, mode)
        total += len(rows)
        for r in rows:
            # Resolve strategy_id from the freqtrade strategy class name
            sname = r.get("strategy_name") or ""
            if sname not in _strategy_id_cache:
                _strategy_id_cache[sname] = _resolve_strategy_id(db, user_id, sname)
            strategy_id = _strategy_id_cache[sname]

            existing = db.execute(
                select(Trade).where(
                    Trade.user_id == user_id,
                    Trade.mode == r["mode"],
                    Trade.pair == r["pair"],
                    Trade.entry_time == r["entry_time"],
                )
            ).scalar_one_or_none()

            if existing:
                existing.exit_price = r["exit_price"]
                existing.profit_pct = r["profit_pct"]
                existing.profit_abs = r["profit_abs"]
                existing.stoploss_price = r["stoploss_price"]
                existing.exit_time = r["exit_time"]
                existing.exit_reason = r["exit_reason"]
                existing.status = r["status"]
                # Update strategy_id if we can resolve it and it's not already set
                if strategy_id and not existing.strategy_id:
                    existing.strategy_id = strategy_id
                updated += 1
            else:
                db.add(
                    Trade(
                        user_id=user_id,
                        mode=r["mode"],
                        pair=r["pair"],
                        side=r["side"],
                        entry_price=r["entry_price"],
                        exit_price=r["exit_price"],
                        amount=r["amount"],
                        profit_pct=r["profit_pct"],
                        profit_abs=r["profit_abs"],
                        stoploss_price=r["stoploss_price"],
                        entry_time=r["entry_time"],
                        exit_time=r["exit_time"],
                        exit_reason=r["exit_reason"],
                        status=r["status"],
                        strategy_id=strategy_id,
                    )
                )
                inserted += 1

    db.commit()
    return {
        "synced": True,
        "user_id": user_id,
        "total_in_freqtrade": total,
        "inserted": inserted,
        "updated": updated,
        "live_db_exists": live_db.exists(),
        "paper_db_exists": dry_db.exists(),
    }
