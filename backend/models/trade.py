from sqlalchemy import Column, Integer, Text, Float, Boolean, DateTime, ForeignKey, CheckConstraint, Index, LargeBinary
from sqlalchemy.sql import func
from .database import Base


class Backtest(Base):
    __tablename__ = "backtests"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, default="local-dev", server_default="local-dev", index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"))
    timerange = Column(Text)
    starting_balance = Column(Float, default=1000)
    total_profit = Column(Float)
    win_rate = Column(Float)
    max_drawdown = Column(Float)
    sharpe_ratio = Column(Float)
    total_trades = Column(Integer)
    avg_duration = Column(Text)
    results_json = Column(Text)
    created_at = Column(DateTime, server_default=func.now())


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, default="local-dev", server_default="local-dev", index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"))
    mode = Column(Text, nullable=False)
    market_type = Column(Text, default="spot", server_default="spot", nullable=True)
    pair = Column(Text, nullable=False)
    side = Column(Text, default="long")
    leverage = Column(Integer, default=1, server_default="1", nullable=True)
    liquidation_price = Column(Float, nullable=True)
    copy_source_id = Column(Integer, nullable=True)
    entry_price = Column(Float)
    exit_price = Column(Float)
    amount = Column(Float)                                 # USDT stake / margin
    profit_pct = Column(Float)
    profit_abs = Column(Float)
    stoploss_price = Column(Float)
    # Take-profit price + ARM runtime snapshot, persisted so a PAPER bot's open
    # position can be rehydrated into its engine after a backend restart (live
    # bots recover from KuCoin; paper had no source of truth and orphaned them).
    tp_price = Column(Float, nullable=True)
    arm_state = Column(Text, nullable=True)               # JSON ARM snapshot or null
    # StrategyInstance.id that opened this trade — lets rehydration claim the
    # exact bot's positions even when two bots run the same strategy template.
    instance_id = Column(Integer, nullable=True, index=True)
    entry_time = Column(DateTime)
    exit_time = Column(DateTime)
    exit_reason = Column(Text)
    status = Column(Text, default="open")

    __table_args__ = (
        CheckConstraint("mode IN ('paper', 'live')"),
        CheckConstraint("side IN ('long', 'short')"),
        CheckConstraint("status IN ('open', 'closed', 'cancelled')"),
    )


class FuturesBacktest(Base):
    """Stores results of a futures backtest run (with leverage, liquidations, shorts)."""
    __tablename__ = "futures_backtests"

    id               = Column(Integer, primary_key=True)
    user_id          = Column(Text, nullable=False, server_default="local-dev", index=True)
    strategy_id      = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    strategy_name    = Column(Text)
    pairs            = Column(Text)          # comma-separated
    timeframe        = Column(Text)
    timerange        = Column(Text)
    leverage         = Column(Integer, default=10)
    starting_balance = Column(Float, default=1000)
    final_balance    = Column(Float)
    total_profit_pct = Column(Float)
    total_profit_abs = Column(Float)
    win_rate         = Column(Float)
    max_drawdown     = Column(Float)
    total_trades     = Column(Integer)
    winning_trades   = Column(Integer)
    losing_trades    = Column(Integer)
    liquidations     = Column(Integer, default=0)
    long_trades      = Column(Integer, default=0)
    short_trades     = Column(Integer, default=0)
    avg_leverage_pnl = Column(Float)         # avg P&L per trade (leveraged)
    results_json     = Column(Text)
    created_at       = Column(DateTime, server_default=func.now())


class StrategyInstance(Base):
    """One running engine instance — enables multi-strategy per user."""
    __tablename__ = "strategy_instances"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    strategy_name = Column(Text, nullable=False)
    market_type = Column(Text, default="spot")    # spot | futures
    mode = Column(Text, default="paper")          # paper | live
    api_mode = Column(Text, default="lead")       # lead | regular (KuCoin futures API)
    pairs = Column(Text, default="BTC/USDT")      # CSV
    leverage = Column(Integer, default=1)
    timeframe = Column(Text, default="15m")
    stoploss = Column(Float, default=-0.03)
    takeprofit = Column(Float, default=0.0)
    wallet = Column(Float, default=1000.0)
    risk_pct = Column(Float, default=5.0)
    is_running = Column(Boolean, default=False)
    # NICE-6 — pause state persists across backend restarts so a bot the
    # user paused doesn't silently un-pause after a Railway redeploy or
    # crash recovery. Auto-resume reads this column and re-applies the
    # paused flag on the engine after start.
    is_paused = Column(Boolean, default=False)
    engine_key = Column(Text, unique=True)        # user_id:instance_id
    total_trades = Column(Integer, default=0)
    total_pnl = Column(Float, default=0.0)
    # ── Advanced Risk Management (Phase 3) ─────────────────────────────
    # Survives backend restart so auto-resume restarts bots with the same
    # ARM config the user picked. Defaults match "ARM off" so pre-existing
    # bots created before this column was added keep their legacy single-TP
    # behaviour unchanged.
    arm_enabled       = Column(Boolean, default=False)
    arm_tp1_close_pct = Column(Float,   default=50.0)
    arm_be_mode       = Column(Text,    default="leverage")
    arm_be_buffer_pct = Column(Float,   default=1.0)
    arm_trail_to_tp1  = Column(Boolean, default=True)
    # ── Per-bot strategy overrides (added 2026-05-24) ──────────────────
    # Surface the user's UI choices for session window + equal-price
    # threshold so auto-resume restarts bots with the same settings the
    # user picked. Without these columns, a backend restart would silently
    # reset a "24/7 session" bot back to NY hours (12-21 UTC). Read by
    # the engine via _strategy_overrides dict; passed to strategies via
    # metadata['overrides'] in evaluate_strategy.
    session_start_hr_utc = Column(Integer, default=12)
    session_end_hr_utc   = Column(Integer, default=21)
    equal_price_thresh   = Column(Float,   default=0.001)
    # ── Position mode (Phase 9 — hedge support, added 2026-06-06) ──────
    # "single" (default) = stop-and-reverse; "hedge" = allow a LONG and a
    # SHORT to coexist on the same pair. Persisted so auto-resume restarts
    # a hedge bot in hedge mode after a backend restart. Default "single"
    # keeps every pre-existing bot's behaviour unchanged.
    position_mode      = Column(Text,    default="single")
    # ── Per-bot strategy flag overrides (UI toggles, added 2026-06-13) ──
    # JSON-encoded dict of boolean strategy options (e.g.
    # {"use_exit_signals": true} for StrategyAsh's CHoCH exit, or the LDC's
    # USE_DYNAMIC_EXITS / USE_ATR_STOPS). NULL = use the strategy's class
    # defaults. Persisted so auto-resume restarts the bot with the same
    # toggles the user picked. _lightweight_migrate auto-adds the column.
    strategy_flags     = Column(Text,    nullable=True)
    # ── SL/TP source (added 2026-06-13) ───────────────────────────────
    # False (default) = use the strategy's structural SL/TP when it provides
    # them, else the slider %s (current behaviour, unchanged). True = force the
    # slider stoploss/takeprofit %s for EVERY trade, ignoring structural levels
    # — the live/paper equivalent of the backtest's "From sliders below"
    # toggle. Persisted so auto-resume keeps the chosen source after a restart.
    force_slider_sltp  = Column(Boolean, default=False)
    # ── Paper-mode cost simulation (added 2026-06-13) ─────────────────
    # False (default) = frictionless paper fills (unchanged). True = deduct
    # simulated KuCoin fees + slippage from paper P&L so paper ≈ live. Paper-
    # only; ignored in live (real fees). Persisted so auto-resume keeps it.
    paper_sim_costs    = Column(Boolean, default=False)
    # ── Consecutive-loss adaptive cooldown (WolfBot-style risk guardrail) ──
    # Pause new entries for `guard_cooldown_min` minutes after `guard_max_consec`
    # losing trades in a row. _lightweight_migrate auto-adds these columns.
    guard_enabled      = Column(Boolean, default=True)
    guard_max_consec   = Column(Integer, default=5)
    guard_cooldown_min = Column(Integer, default=60)
    # Max NEW entries per UTC day. Persisted so it survives a restart — before
    # this column existed, resume never passed it and the engine re-defaulted to
    # 8, silently capping bots at 8 trades/day. Default 999 = effectively
    # unlimited. _lightweight_migrate auto-adds the column.
    max_trades_per_day = Column(Integer, default=999)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class MLFilterModel(Base):
    """Trained per-strategy ML loss-filter (meta-labeling take/skip model).
    Stored only when it PASSES the walk-forward gate. The engine/backtester load
    the latest passing model for a (user, strategy) and skip low-confidence
    signals. model_blob = joblib-serialized {model, features, mu, sd, conf}."""
    __tablename__ = "ml_filter_models"

    id           = Column(Integer, primary_key=True)
    user_id      = Column(Text, nullable=False, index=True)
    strategy_id  = Column(Integer, ForeignKey("strategies.id"), index=True)
    context      = Column(Text)             # e.g. "BTC,ETH,SOL · 1h"
    model_blob   = Column(LargeBinary)       # joblib bytes
    metrics      = Column(Text)              # JSON: walk-forward report
    verdict      = Column(Text)              # "PASS" / "FAIL"
    signals      = Column(Integer)           # # training signals
    conf         = Column(Float, default=0.55)
    enabled      = Column(Boolean, default=False)   # user opt-in to use it live
    created_at   = Column(DateTime, server_default=func.now())


class FuturesOrder(Base):
    """Pending/active futures orders — limit, stop, conditional, trailing stop."""
    __tablename__ = "futures_orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Text, nullable=False, index=True)
    # 'paper' | 'live'. Default 'paper' is conservative — orders without an
    # explicit mode never accidentally appear in Live tab. Critical for
    # isolation: without this column, a paper LIMIT BUY at 81058 was leaking
    # into the Live mode Open Orders panel even though it was clearly a
    # simulated order.
    mode = Column(Text, nullable=False, default="paper", server_default="paper", index=True)
    symbol = Column(Text, nullable=False)
    side = Column(Text, nullable=False)
    order_type = Column(Text, nullable=False, default="limit")
    size = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    leverage = Column(Integer, default=1)
    margin_mode = Column(Text, default="cross")
    client_oid = Column(Text, nullable=True)
    exchange_order_id = Column(Text, nullable=True)
    status = Column(Text, default="pending")
    time_in_force = Column(Text, default="GTC")
    hidden = Column(Boolean, default=False)
    post_only = Column(Boolean, default=False)
    reduce_only = Column(Boolean, default=False)
    close_order = Column(Boolean, default=False)
    tp_price = Column(Float, nullable=True)
    sl_price = Column(Float, nullable=True)
    filled_size = Column(Float, default=0)
    filled_price = Column(Float, nullable=True)
    fee = Column(Float, default=0)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    filled_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("side IN ('buy', 'sell')"),
        CheckConstraint("order_type IN ('limit', 'market', 'stop', 'stop_limit', 'trailing_stop', 'twap', 'stop_tp', 'stop_sl')"),
        CheckConstraint("status IN ('pending', 'active', 'filled', 'partially_filled', 'cancelled', 'triggered')"),
        CheckConstraint("margin_mode IN ('cross', 'isolated')"),
        CheckConstraint("mode IN ('paper', 'live')"),
        Index("ix_futures_orders_user_status", "user_id", "status"),
        Index("ix_futures_orders_user_mode", "user_id", "mode"),
    )


class CopySignal(Base):
    """Trade signal broadcast by a master trader."""
    __tablename__ = "copy_signals"

    id = Column(Integer, primary_key=True)
    master_user_id = Column(Text, nullable=False, index=True)
    pair = Column(Text, nullable=False)
    direction = Column(Text, default="long")      # long | short
    market_type = Column(Text, default="spot")    # spot | futures
    leverage = Column(Integer, default=1)
    entry_price = Column(Float)
    sl_price = Column(Float)
    tp_price = Column(Float)
    stake_pct = Column(Float, default=5.0)
    strategy_name = Column(Text)
    signal_type = Column(Text, default="entry")   # entry | exit
    profit_pct = Column(Float)
    profit_abs = Column(Float)
    broadcasted_at = Column(DateTime, server_default=func.now())
    expires_at = Column(DateTime)
    closed_at = Column(DateTime)


class CopySubscription(Base):
    """Follower ➜ Master subscription."""
    __tablename__ = "copy_subscriptions"

    id = Column(Integer, primary_key=True)
    follower_user_id = Column(Text, nullable=False, index=True)
    master_user_id = Column(Text, nullable=False, index=True)
    is_active = Column(Boolean, default=True)
    copy_mode = Column(Text, default="paper")     # paper | live
    copy_market_type = Column(Text, default="spot")
    max_leverage = Column(Integer, default=10)
    stake_override_pct = Column(Float)            # null = use master's stake
    total_copied = Column(Integer, default=0)
    total_profit = Column(Float, default=0.0)
    win_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
