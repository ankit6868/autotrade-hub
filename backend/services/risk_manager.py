import json
from datetime import datetime, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from backend.models.trade import Trade
from backend.models.config import Config


class RiskManager:
    def __init__(self, config: Config):
        self.max_position_pct = config.max_position_pct
        self.max_open_trades = config.max_open_trades
        self.max_daily_drawdown_pct = config.max_daily_drawdown_pct
        self.default_stoploss_pct = config.default_stoploss_pct

    def check_can_open_trade(self, db: Session, mode: str, user_id: str, wallet: float = 1000.0) -> dict:
        """Check if a new trade can be opened based on risk rules.

        `wallet` is the starting balance used to translate absolute P&L into a
        percentage for the drawdown check. For paper trades callers should
        pass the configured dry_run_wallet; for live trades the current equity.
        """
        # Count open trades for THIS user only
        result = db.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == user_id,
                Trade.mode == mode,
                Trade.status == "open",
            )
        )
        open_count = result.scalar() or 0

        if open_count >= self.max_open_trades:
            return {"allowed": False, "reason": f"Max open trades ({self.max_open_trades}) reached"}

        # Check daily drawdown for THIS user only
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        result = db.execute(
            select(func.sum(Trade.profit_abs)).where(
                Trade.user_id == user_id,
                Trade.mode == mode,
                Trade.status == "closed",
                Trade.exit_time >= today,
            )
        )
        daily_pnl = result.scalar() or 0
        daily_pnl_pct = (daily_pnl / wallet * 100.0) if wallet else 0.0

        if daily_pnl_pct < 0 and abs(daily_pnl_pct) >= self.max_daily_drawdown_pct:
            return {
                "allowed": False,
                "reason": f"Daily drawdown limit ({self.max_daily_drawdown_pct}%) breached ({daily_pnl_pct:.2f}%)",
            }

        return {
            "allowed": True,
            "open_trades": open_count,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl_pct,
        }

    def calculate_position_size(self, balance: float, entry_price: float) -> float:
        """Calculate position size based on max position percentage."""
        max_amount = balance * (self.max_position_pct / 100)
        return max_amount / entry_price

    def calculate_stoploss_price(self, entry_price: float, side: str = "long") -> float:
        """Calculate stop-loss price."""
        if side == "long":
            return entry_price * (1 - self.default_stoploss_pct / 100)
        return entry_price * (1 + self.default_stoploss_pct / 100)

    def check_live_safety(self, db: Session, user_id: str) -> dict:
        """Check all safety gates for live trading. All checks are scoped to this user only."""
        errors = []

        # Check paper trading history (7 days) for THIS user
        result = db.execute(
            select(func.min(Trade.entry_time)).where(
                Trade.user_id == user_id,
                Trade.mode == "paper",
            )
        )
        first_paper = result.scalar()
        if not first_paper:
            errors.append("No paper trading history found. Paper trade for at least 7 days first.")
        elif (datetime.utcnow() - first_paper).days < 7:
            days = (datetime.utcnow() - first_paper).days
            errors.append(f"Only {days} days of paper trading. Need at least 7 days.")

        # Check paper trading profitability for THIS user
        result = db.execute(
            select(func.sum(Trade.profit_abs)).where(
                Trade.user_id == user_id,
                Trade.mode == "paper",
                Trade.status == "closed",
            )
        )
        paper_pnl = result.scalar() or 0
        if paper_pnl <= 0:
            errors.append(f"Paper trading is not profitable (P&L: {paper_pnl:.2f}). Must be positive.")

        return {
            "safe": len(errors) == 0,
            "errors": errors,
            "paper_days": (datetime.utcnow() - first_paper).days if first_paper else 0,
            "paper_pnl": paper_pnl,
        }
