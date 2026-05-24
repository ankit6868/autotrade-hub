"""
Full platform diagnostic — feature × component × engine matrix.

Runs every critical code path the user touches, reports PASS/FAIL with
the failure detail. Exit code 0 only if everything passes.

Usage:
    cd C:\\Users\\Ankit\\Desktop\\tradebot
    python -X utf8 -m scripts.full_diagnostic
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# ── Test infrastructure ────────────────────────────────────────────────
RESULTS: list[tuple[str, str, bool, str]] = []   # (section, name, ok, detail)


def banner(title: str) -> None:
    print()
    print("=" * 78)
    print(f"  {title}")
    print("=" * 78)


def t(section: str, name: str, fn):
    """Run a test fn, capture pass/fail + exception detail."""
    try:
        detail = fn() or ""
        ok = True
    except AssertionError as e:
        ok, detail = False, f"assert: {e}"
    except Exception:
        ok, detail = False, f"exc: {traceback.format_exc(limit=2).splitlines()[-1][:80]}"
    marker = "PASS" if ok else "FAIL"
    print(f"  [{marker}] {name:55} {detail}")
    RESULTS.append((section, name, ok, detail))


# ────────────────────────────────────────────────────────────────────────
# SECTION 1 — Module imports
# ────────────────────────────────────────────────────────────────────────
def section_imports():
    banner("SECTION 1 — Backend module imports")
    import importlib
    for mod in [
        "backend.main",
        "backend.models.strategy",
        "backend.models.trade",
        "backend.services.strategy_runner",
        "backend.services.strategy_validator",
        "backend.services.strategy_template",
        "backend.services.guided_strategy_builder",
        "backend.services.mtf_analyzer",
        "backend.services.mtf_candles",
        "backend.services.timeframe_adapter",
        "backend.services.futures_backtester",
        "backend.services.futures_engine",
        "backend.services.paper_scalp_engine",
        "backend.services.kucoin_futures_client",
        "backend.services.kucoin_ws_futures",
        "backend.services.live_bar_aggregator",
        "backend.services.native_backtester",
        "backend.services.native_trading_engine",
        "backend.routers.futures",
        "backend.routers.strategy",
        "backend.routers.paper_scalp",
        "backend.routers.market",
        "backend.routers.auth",
    ]:
        t("imports", mod, lambda m=mod: importlib.import_module(m) and None)


# ────────────────────────────────────────────────────────────────────────
# SECTION 2 — Strategy compile + evaluate
# ────────────────────────────────────────────────────────────────────────
def section_strategies():
    banner("SECTION 2 — Strategy compile + evaluate (synthetic)")
    import pandas as pd, numpy as np
    import backend.main as m
    from backend.services.strategy_runner import evaluate_strategy

    n = 400
    dates = pd.date_range("2024-01-01", periods=n, freq="15min", tz="UTC")
    np.random.seed(11)
    close = 70000 + np.cumsum(np.random.randn(n) * 60)
    df = pd.DataFrame({
        "date": dates, "open": close + np.random.randn(n)*20,
        "high": close + np.abs(np.random.randn(n)*50),
        "low":  close - np.abs(np.random.randn(n)*50),
        "close": close, "vol": np.abs(np.random.randn(n)*100) + 100,
    })

    templates = [
        ("_SMC_TV_STRATEGY_CODE",         "SMCStrategyTV"),
        ("_SMC_STRATEGY_CODE",            "SMCStrategy"),
        ("_SIMPLE_STRATEGY_CODE",         "SimpleTargetStrategy"),
        ("_BIDIR_STRATEGY_CODE",          "BidirectionalStrategy"),
        ("_SMC_PRO_V3_CODE",              "SMCProV3"),
        ("_BESTPRACTICES_V1_CODE",        "BestPracticesV1"),
        ("_BESTPRACTICES_V1_STRICT_CODE", "BestPracticesV1Strict"),
        ("_SMC_STRATEGY_1_CODE",          "SMCStrategy1"),
        ("_STRATEGY_ASH_CODE",            "StrategyAsh"),
    ]
    for const, name in templates:
        def run(c=const, n_=name):
            code = getattr(m, c, None)
            assert code, f"constant {c} missing"
            out = evaluate_strategy(code, df.copy(), pair="BTC/USDT", execution_tf="15m")
            assert "enter_long" in out.columns,  "enter_long column missing"
            assert "enter_short" in out.columns, "enter_short column missing"
            return f"signals L/S = {int(out['enter_long'].sum())}/{int(out['enter_short'].sum())}"
        t("strategies", name, run)


# ────────────────────────────────────────────────────────────────────────
# SECTION 3 — Validator + live-permission
# ────────────────────────────────────────────────────────────────────────
def section_validator():
    banner("SECTION 3 — Validator + live guardrail")
    import backend.main as m
    from backend.services.strategy_validator import validate_for_live

    for const, name in [
        ("_SMC_STRATEGY_1_CODE", "SMCStrategy1"),
        ("_STRATEGY_ASH_CODE",   "StrategyAsh"),
        ("_BESTPRACTICES_V1_CODE","BestPracticesV1"),
    ]:
        def run(c=const, n_=name):
            code = getattr(m, c)
            ok, tpl, _ = validate_for_live(
                strategy_name=n_, strategy_id=1,
                generated_code=code, execution_timeframe="15m",
            )
            assert tpl.confidence_score > 0, f"confidence=0 for {n_}"
            return f"score={tpl.confidence_score} perm={tpl.live_permission} max_trades={tpl.trade_limits.max_trades_per_day}"
        t("validator", name, run)


# ────────────────────────────────────────────────────────────────────────
# SECTION 4 — TF + MTF wiring
# ────────────────────────────────────────────────────────────────────────
def section_tf_wiring():
    banner("SECTION 4 — TF + MTF wiring")
    from backend.services.mtf_analyzer import get_strategy_bias_tfs, get_strategy_sub_tfs
    from backend.services.mtf_candles import _tf_seconds
    from backend.services.live_bar_aggregator import TF_SECS_MAP

    t("tf-wiring", "_tf_seconds(1m)=60",   lambda: f"{_tf_seconds('1m')}"
            if _tf_seconds("1m") == 60 else (_ for _ in ()).throw(AssertionError("got {}".format(_tf_seconds("1m")))))
    t("tf-wiring", "_tf_seconds(4h)=14400", lambda: f"{_tf_seconds('4h')}"
            if _tf_seconds("4h") == 14400 else (_ for _ in ()).throw(AssertionError("got {}".format(_tf_seconds("4h")))))
    t("tf-wiring", "TF_SECS_MAP[5m]=300",   lambda: f"{TF_SECS_MAP['5m']}"
            if TF_SECS_MAP.get("5m") == 300 else (_ for _ in ()).throw(AssertionError("got {}".format(TF_SECS_MAP.get("5m")))))

    class S1:  # bias only
        bias_timeframes = ["1h", "4h"]
    class S2:  # both
        bias_timeframes = ["1h", "4h"]
        sub_timeframes  = ["1m", "3m"]
    class S3:  # neither
        pass

    t("tf-wiring", "bias filter drops <= exec",
        lambda: f"{get_strategy_bias_tfs(S1, '4h')}" if get_strategy_bias_tfs(S1, "4h") == [] else (_ for _ in ()).throw(AssertionError(f"got {get_strategy_bias_tfs(S1, '4h')}")))
    t("tf-wiring", "sub filter drops >= exec",
        lambda: f"{get_strategy_sub_tfs(S2, '1m')}" if get_strategy_sub_tfs(S2, "1m") == [] else (_ for _ in ()).throw(AssertionError(f"got {get_strategy_sub_tfs(S2, '1m')}")))
    t("tf-wiring", "bias kept when > exec",
        lambda: f"{get_strategy_bias_tfs(S1, '5m')}" if get_strategy_bias_tfs(S1, "5m") == ["1h", "4h"] else (_ for _ in ()).throw(AssertionError("filter wrong")))
    t("tf-wiring", "sub kept when < exec",
        lambda: f"{get_strategy_sub_tfs(S2, '5m')}"  if get_strategy_sub_tfs(S2, "5m") == ["1m", "3m"] else (_ for _ in ()).throw(AssertionError("filter wrong")))
    t("tf-wiring", "no decl → both empty",
        lambda: "OK" if (get_strategy_bias_tfs(S3, "15m") == [] and get_strategy_sub_tfs(S3, "15m") == []) else (_ for _ in ()).throw(AssertionError("not both empty")))


# ────────────────────────────────────────────────────────────────────────
# SECTION 5 — Backtest engine wiring
# ────────────────────────────────────────────────────────────────────────
def section_backtest():
    banner("SECTION 5 — Backtest engine — full flow on real KuCoin data")
    import datetime as dt
    from backend.services.futures_backtester import run_futures_backtest
    import backend.main as m
    end_ts   = int(time.time())
    start_ts = end_ts - 7 * 24 * 3600     # 1 week
    s = dt.datetime.utcfromtimestamp(start_ts).strftime("%Y%m%d")
    e = dt.datetime.utcfromtimestamp(end_ts).strftime("%Y%m%d")
    trange = f"{s}-{e}"

    def run_one(name, code, tf):
        result = run_futures_backtest(
            strategy_name=name, pairs=["BTC/USDT"], timeframe=tf,
            timerange=trange, generated_code=code,
            leverage=10, starting_balance=1000.0,
            risk_per_trade=0.05, max_concurrent_positions=1,
            deduct_real_costs=False,
        )
        assert "error" not in result, result.get("error", "")
        m_ = result.get("metrics", {})
        return f"{m_.get('total_trades', 0)} trades, WR={m_.get('win_rate',0)*100:.1f}%, P&L={m_.get('total_profit_pct',0):.2f}%"

    t("backtest", "SMCStrategyTV @ 15m",  lambda: run_one("SMCStrategyTV",  m._SMC_TV_STRATEGY_CODE,  "15m"))
    t("backtest", "BestPracticesV1 @ 15m", lambda: run_one("BestPracticesV1", m._BESTPRACTICES_V1_CODE, "15m"))
    t("backtest", "SMCStrategy1 @ 15m",   lambda: run_one("SMCStrategy1",   m._SMC_STRATEGY_1_CODE,   "15m"))
    t("backtest", "StrategyAsh @ 5m",     lambda: run_one("StrategyAsh",    m._STRATEGY_ASH_CODE,     "5m"))


# ────────────────────────────────────────────────────────────────────────
# SECTION 6 — Database model integrity
# ────────────────────────────────────────────────────────────────────────
def section_db_models():
    banner("SECTION 6 — DB models — required columns present")
    from backend.models.trade import StrategyInstance, FuturesOrder, FuturesBacktest, Trade

    def check_cols(cls, required):
        cols = {c.name for c in cls.__table__.columns}
        missing = [r for r in required if r not in cols]
        assert not missing, f"missing: {missing}"
        return f"all {len(required)} cols present"

    t("db-models", "StrategyInstance ARM cols",
        lambda: check_cols(StrategyInstance, ["arm_enabled","arm_tp1_close_pct","arm_be_mode","arm_be_buffer_pct","arm_trail_to_tp1"]))
    t("db-models", "StrategyInstance overrides cols",
        lambda: check_cols(StrategyInstance, ["session_start_hr_utc","session_end_hr_utc","equal_price_thresh"]))
    t("db-models", "StrategyInstance core cols",
        lambda: check_cols(StrategyInstance, ["user_id","strategy_id","strategy_name","pairs","leverage","timeframe","mode","is_running","engine_key"]))
    t("db-models", "FuturesOrder mode col",
        lambda: check_cols(FuturesOrder, ["mode"]))
    t("db-models", "FuturesBacktest core cols",
        lambda: check_cols(FuturesBacktest, ["user_id","strategy_name","pairs","timeframe","leverage","total_trades","win_rate"]))


# ────────────────────────────────────────────────────────────────────────
# SECTION 7 — API surface (routes registered, request body shapes)
# ────────────────────────────────────────────────────────────────────────
def section_api():
    banner("SECTION 7 — API surface — every endpoint reachable")
    from backend.main import app

    routes = set()
    for r in app.routes:
        if hasattr(r, "methods") and hasattr(r, "path"):
            for m_ in r.methods:
                if m_ not in ("HEAD", "OPTIONS"):
                    routes.add(f"{m_} {r.path}")

    required = [
        "POST /api/futures/backtest/run",
        "POST /api/futures/bots",
        "GET /api/futures/bots",
        "DELETE /api/futures/bots/{bot_id}",
        "POST /api/futures/bots/{bot_id}/pause",
        "POST /api/futures/bots/{bot_id}/resume",
        "GET /api/futures/bots/{bot_id}/performance",
        "GET /api/futures/lead-trading-status",
        "GET /api/futures/balance",
        "GET /api/futures/open",
        "GET /api/futures/orders",
        "POST /api/futures/order",
        "POST /api/futures/manual-entry",
        "POST /api/futures/leverage",
        "POST /api/futures/margin-mode",
        "POST /api/futures/position/tp-sl",
        "POST /api/futures/position/partial-close",
        "POST /api/futures/force-close/{pair:path}",
        "POST /api/paper-scalp/start",
        "POST /api/paper-scalp/stop",
        "GET /api/paper-scalp/status",
        "GET /api/strategy/list",
        "GET /api/strategy/templates",
        "POST /api/strategy/parse",
        "POST /api/strategy/ai-assist",
        "GET /api/strategy/{strategy_id}/preview",
        "GET /api/strategy/{strategy_id}/validate",
    ]
    for r in required:
        t("api", r, lambda r_=r: "registered" if r_ in routes else (_ for _ in ()).throw(AssertionError("missing")))


# ────────────────────────────────────────────────────────────────────────
# SECTION 8 — Strategy class attrs surfaced to compiled_df.attrs
# ────────────────────────────────────────────────────────────────────────
def section_class_attrs():
    banner("SECTION 8 — Strategy class attrs surface to compiled_df.attrs")
    import pandas as pd
    import backend.main as m
    from backend.services.strategy_runner import evaluate_strategy

    dummy = pd.DataFrame({
        "date":  pd.date_range("2024-01-01", periods=100, freq="15min", tz="UTC"),
        "open": [100]*100, "high":[101]*100, "low":[99]*100,
        "close":[100]*100, "vol":[1000]*100,
    })

    def check_attrs(code_const, expected_keys):
        code = getattr(m, code_const)
        out = evaluate_strategy(code, dummy.copy(), pair="BTC/USDT", execution_tf="15m")
        missing = [k for k in expected_keys if k not in out.attrs]
        assert not missing, f"missing attrs: {missing}"
        return f"{len(expected_keys)} attrs surfaced"

    t("class-attrs", "StrategyAsh — opt-in gates",
        lambda: check_attrs("_STRATEGY_ASH_CODE",
                            ["class_max_hold_candles","class_max_stops_per_day","class_sub_timeframes"]))
    t("class-attrs", "SMCStrategy1 — bias_timeframes via class",
        lambda: check_attrs("_SMC_STRATEGY_1_CODE",
                            ["strategy_class"]))


# ────────────────────────────────────────────────────────────────────────
# SECTION 9 — Engine config plumbing (futures_engine.start_futures)
# ────────────────────────────────────────────────────────────────────────
def section_engine_plumbing():
    banner("SECTION 9 — Engine config plumbing (kwargs accepted)")
    import inspect
    from backend.services.futures_engine import FuturesEngine
    sig = inspect.signature(FuturesEngine.start_futures)
    params = set(sig.parameters)

    required = [
        # Core
        "strategy_name","pairs","leverage","mode","timeframe","stoploss","wallet",
        # Phase 3 ARM
        "arm_enabled","arm_tp1_close_pct","arm_be_mode","arm_be_buffer_pct","arm_trail_to_tp1",
        # Phase 8 risk
        "max_trades_per_day","cooldown_candles","max_daily_dd_pct",
        # Per-bot overrides (latest commit)
        "session_start_hr_utc","session_end_hr_utc","equal_price_thresh",
        # Opt-in gates
        "max_hold_candles","max_stops_per_day",
    ]
    for p in required:
        t("engine-plumbing", f"start_futures({p}=...)",
            lambda p_=p: "kw accepted" if p_ in params else (_ for _ in ()).throw(AssertionError(f"missing kwarg")))


# ────────────────────────────────────────────────────────────────────────
# SECTION 10 — WebSocket scaffolding (no network, just shape)
# ────────────────────────────────────────────────────────────────────────
def section_ws():
    banner("SECTION 10 — WebSocket client + bar aggregator (shape only)")
    from backend.services.kucoin_ws_futures import KuCoinFuturesWSClient, PING_INTERVAL_SECS, RECONNECT_BACKOFF
    from backend.services.live_bar_aggregator import LiveBarAggregator, Bar, TF_SECS_MAP

    t("websocket", "KuCoinFuturesWSClient instantiable",
        lambda: "OK" if KuCoinFuturesWSClient() is not None else (_ for _ in ()).throw(AssertionError("init failed")))
    t("websocket", "PING_INTERVAL_SECS sane",
        lambda: f"{PING_INTERVAL_SECS}s" if 5 <= PING_INTERVAL_SECS <= 18 else (_ for _ in ()).throw(AssertionError(f"got {PING_INTERVAL_SECS}")))
    t("websocket", "RECONNECT_BACKOFF non-empty",
        lambda: f"{len(RECONNECT_BACKOFF)} steps" if len(RECONNECT_BACKOFF) > 0 else (_ for _ in ()).throw(AssertionError("empty")))
    t("websocket", "LiveBarAggregator(1m) builds",
        lambda: "tf_secs=60" if LiveBarAggregator("XBTUSDTM","1m").tf_secs == 60 else (_ for _ in ()).throw(AssertionError("wrong tf_secs")))


# ────────────────────────────────────────────────────────────────────────
# Driver
# ────────────────────────────────────────────────────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    section_imports()
    section_strategies()
    section_validator()
    section_tf_wiring()
    section_db_models()
    section_api()
    section_class_attrs()
    section_engine_plumbing()
    section_ws()
    section_backtest()         # network — run last so a flaky net doesn't mask other failures

    banner("SUMMARY")
    by_section: dict[str, list] = {}
    for sec, _, ok, _ in RESULTS:
        by_section.setdefault(sec, []).append(ok)
    overall_ok = all(ok for *_, ok, _ in RESULTS)
    print()
    print(f"  {'Section':<22} {'Passed':>10} {'Total':>8}")
    print("  " + "-" * 42)
    for sec, oks in by_section.items():
        passed = sum(1 for o in oks if o)
        total  = len(oks)
        print(f"  {sec:<22} {passed:>10} {total:>8}")
    print()
    print(f"  OVERALL: {'ALL CHECKS PASSED' if overall_ok else 'FAILURES — see above'}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
