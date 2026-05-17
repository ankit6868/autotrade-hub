"""
Strategy runner — executes a user-defined Freqtrade IStrategy class against a
pandas DataFrame and returns the dataframe with entry/exit signal columns
populated.

Why this exists
---------------
The user authors strategies in natural language; the LLM converts them to
Freqtrade-style Python code (an IStrategy subclass with populate_indicators,
populate_entry_trend, populate_exit_trend), stored in Strategy.generated_code.

Before this module, the futures backtester only pattern-matched on the
strategy's *name* and ran one of the hardcoded built-in signal functions
(SMC, MACD, RSI-Bollinger, etc.). Two consequences:

  1. Custom strategies were silently ignored — their generated_code never
     ran. A strategy named "My Strategy" got mapped to SimpleTarget.
  2. Strategies named after built-ins (e.g. an LLM-generated SMC variant)
     ran the hardcoded SMC function instead of the user's actual logic.

This module fixes both: when generated_code is present, we exec it in a
sandbox, instantiate the IStrategy subclass, call populate_indicators +
populate_entry_trend + populate_exit_trend, and read the
enter_long / enter_short / exit_long / exit_short signal columns. The
backtest engine then iterates those signals bar-by-bar as before.

The sandbox provides stubs for freqtrade.strategy.IStrategy and the typical
TA imports (talib, pandas_ta, qtpylib) so user code doesn't need those
packages installed on Railway.
"""
from __future__ import annotations

import logging
import types
from typing import Any

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ── Minimal IStrategy stub the user's code subclasses ──────────────────────
class IStrategy:
    """Mock Freqtrade IStrategy base class.

    The real IStrategy has dozens of methods and configuration hooks; we only
    need the four methods the user's code overrides. Class attributes like
    `timeframe`, `stoploss`, `minimal_roi` are read by our outer engine
    from the Strategy DB row, not from the class — so we don't need to
    honour them here.
    """

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    # Some Freqtrade strategies override the older buy/sell trend hooks.
    def populate_buy_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def populate_sell_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe


def _build_qtpylib_stub() -> types.ModuleType:
    """Provide the handful of qtpylib helpers the LLM commonly emits."""
    mod = types.ModuleType("qtpylib")

    def crossed_above(a: pd.Series, b) -> pd.Series:
        b_series = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
        prev_a = a.shift(1)
        prev_b = b_series.shift(1)
        return (a > b_series) & (prev_a <= prev_b)

    def crossed_below(a: pd.Series, b) -> pd.Series:
        b_series = b if isinstance(b, pd.Series) else pd.Series(b, index=a.index)
        prev_a = a.shift(1)
        prev_b = b_series.shift(1)
        return (a < b_series) & (prev_a >= prev_b)

    mod.crossed_above = crossed_above
    mod.crossed_below = crossed_below
    mod.indicators = types.ModuleType("qtpylib.indicators")
    mod.indicators.crossed_above = crossed_above
    mod.indicators.crossed_below = crossed_below
    return mod


def _build_talib_stub() -> types.ModuleType:
    """Minimal TA-Lib subset implemented in pandas. The LLM emits these names
    most often: RSI, EMA, SMA, MACD, BBANDS, ATR, ADX, STOCH."""
    mod = types.ModuleType("talib")

    def _to_series(x) -> pd.Series:
        if isinstance(x, pd.DataFrame):
            return x["close"]
        if isinstance(x, np.ndarray):
            return pd.Series(x)
        return x

    def SMA(close, timeperiod: int = 14):
        s = _to_series(close)
        return s.rolling(timeperiod).mean()

    def EMA(close, timeperiod: int = 14):
        s = _to_series(close)
        return s.ewm(span=timeperiod, adjust=False).mean()

    def RSI(close, timeperiod: int = 14):
        s = _to_series(close)
        delta = s.diff()
        gain = delta.clip(lower=0).ewm(com=timeperiod - 1, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(com=timeperiod - 1, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-9)
        return 100 - 100 / (1 + rs)

    def MACD(close, fastperiod: int = 12, slowperiod: int = 26, signalperiod: int = 9):
        s = _to_series(close)
        ef = s.ewm(span=fastperiod, adjust=False).mean()
        es = s.ewm(span=slowperiod, adjust=False).mean()
        macd = ef - es
        signal = macd.ewm(span=signalperiod, adjust=False).mean()
        hist = macd - signal
        # Freqtrade strategies use `ta.MACD(df)["macd"]` (dict-style key
        # access on the returned DataFrame). Returning a tuple breaks
        # `macd["macd"]` with TypeError. DataFrame supports both styles.
        return pd.DataFrame({
            "macd":       macd,
            "macdsignal": signal,
            "macdhist":   hist,
        })

    def BBANDS(close, timeperiod: int = 20, nbdevup: float = 2, nbdevdn: float = 2, matype: int = 0):
        s = _to_series(close)
        mid = s.rolling(timeperiod).mean()
        std = s.rolling(timeperiod).std()
        # Freqtrade uses bb["upperband"], bb["middleband"], bb["lowerband"]
        return pd.DataFrame({
            "upperband":  mid + nbdevup * std,
            "middleband": mid,
            "lowerband":  mid - nbdevdn * std,
        })

    def ATR(high, low, close, timeperiod: int = 14):
        h, l, c = _to_series(high), _to_series(low), _to_series(close)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        return tr.rolling(timeperiod).mean()

    def ADX(high, low, close, timeperiod: int = 14):
        # Simplified ADX — Wilder's smoothing approximated by EWM.
        h, l, c = _to_series(high), _to_series(low), _to_series(close)
        up = h.diff()
        dn = -l.diff()
        plus_dm = up.where((up > dn) & (up > 0), 0.0)
        minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
        tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1 / timeperiod, adjust=False).mean()
        plus_di = 100 * plus_dm.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)
        minus_di = 100 * minus_dm.ewm(alpha=1 / timeperiod, adjust=False).mean() / atr.replace(0, 1e-9)
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
        return dx.ewm(alpha=1 / timeperiod, adjust=False).mean()

    def STOCH(high, low, close, fastk_period: int = 5, slowk_period: int = 3, slowk_matype: int = 0,
              slowd_period: int = 3, slowd_matype: int = 0):
        h, l, c = _to_series(high), _to_series(low), _to_series(close)
        ll = l.rolling(fastk_period).min()
        hh = h.rolling(fastk_period).max()
        fastk = 100 * (c - ll) / (hh - ll).replace(0, 1e-9)
        slowk = fastk.rolling(slowk_period).mean()
        slowd = slowk.rolling(slowd_period).mean()
        return slowk, slowd

    for name, fn in dict(
        SMA=SMA, EMA=EMA, RSI=RSI, MACD=MACD, BBANDS=BBANDS,
        ATR=ATR, ADX=ADX, STOCH=STOCH,
    ).items():
        setattr(mod, name, fn)
    return mod


def _build_sandbox() -> dict[str, Any]:
    """Construct the namespace the user's strategy code is exec'd in."""
    # Freqtrade replacement modules
    freqtrade = types.ModuleType("freqtrade")
    freqtrade.strategy = types.ModuleType("freqtrade.strategy")
    freqtrade.strategy.IStrategy = IStrategy
    freqtrade.strategy.IntParameter = lambda *a, **kw: a[0] if a else 0
    freqtrade.strategy.DecimalParameter = lambda *a, **kw: a[0] if a else 0.0
    freqtrade.strategy.CategoricalParameter = lambda *a, **kw: (a[0][0] if a and a[0] else None)
    freqtrade.strategy.BooleanParameter = lambda *a, **kw: a[0] if a else False
    freqtrade.exchange = types.ModuleType("freqtrade.exchange")
    freqtrade.exchange.timeframe_to_minutes = lambda tf: {"1m": 1, "5m": 5, "15m": 15, "30m": 30,
                                                          "1h": 60, "4h": 240, "1d": 1440}.get(tf, 15)
    freqtrade.persistence = types.ModuleType("freqtrade.persistence")

    class _Trade:
        pass
    freqtrade.persistence.Trade = _Trade

    return {
        # When Python defines a class, it reads __name__ from the current
        # module's globals to set the class's __module__ attribute. Setting
        # this lets us cleanly distinguish user-defined classes from
        # imported ones when we hunt for the IStrategy subclass.
        "__name__":    "user_strategy",
        # Standard libs the LLM uses
        "pd":          pd,
        "pandas":      pd,
        "np":          np,
        "numpy":       np,
        "DataFrame":   pd.DataFrame,
        "Series":      pd.Series,
        # Stubs in import-able form via __import__ hook below
        "__builtins__": _safe_builtins(freqtrade),
        # Direct top-level injections so `IStrategy` etc. resolve even if the
        # user wrote `from freqtrade.strategy import IStrategy`.
        "IStrategy":   IStrategy,
    }


def _safe_builtins(freqtrade_mod) -> dict[str, Any]:
    """Restricted builtins. Block file/network/process access but let the
    user's strategy import the standard scientific-Python ecosystem and our
    freqtrade stub."""
    import builtins as _b
    allowed = {
        # Common safe names
        "abs": _b.abs, "all": _b.all, "any": _b.any, "bool": _b.bool,
        "dict": _b.dict, "enumerate": _b.enumerate, "filter": _b.filter,
        "float": _b.float, "int": _b.int, "isinstance": _b.isinstance,
        "len": _b.len, "list": _b.list, "map": _b.map, "max": _b.max,
        "min": _b.min, "next": _b.next, "print": _b.print, "range": _b.range,
        "round": _b.round, "set": _b.set, "slice": _b.slice, "sorted": _b.sorted,
        "str": _b.str, "sum": _b.sum, "tuple": _b.tuple, "type": _b.type,
        "zip": _b.zip, "iter": _b.iter, "object": _b.object, "True": True,
        "False": False, "None": None, "getattr": _b.getattr, "setattr": _b.setattr,
        "hasattr": _b.hasattr, "Exception": _b.Exception, "ValueError": _b.ValueError,
        "TypeError": _b.TypeError, "KeyError": _b.KeyError, "AttributeError": _b.AttributeError,
        "min": _b.min, "max": _b.max, "callable": _b.callable, "vars": _b.vars,
        "property": _b.property, "super": _b.super, "staticmethod": _b.staticmethod,
        "classmethod": _b.classmethod, "id": _b.id, "repr": _b.repr,
        # Class machinery — Python's `class Foo: ...` syntax compiles to a
        # call to __build_class__. Without it, every IStrategy subclass
        # definition fails with "__build_class__ not found".
        "__build_class__": _b.__build_class__,
        "__name__": "user_strategy",
        # Additional commonly-used safe builtins by LLM-generated code.
        "frozenset": _b.frozenset, "format": _b.format, "ord": _b.ord,
        "chr": _b.chr, "divmod": _b.divmod, "pow": _b.pow, "hex": _b.hex,
        "oct": _b.oct, "bin": _b.bin, "complex": _b.complex, "bytes": _b.bytes,
        "bytearray": _b.bytearray, "reversed": _b.reversed,
        "RuntimeError": _b.RuntimeError, "ZeroDivisionError": _b.ZeroDivisionError,
        "IndexError": _b.IndexError, "StopIteration": _b.StopIteration,
        "NotImplementedError": _b.NotImplementedError,
        "NotImplemented": _b.NotImplemented,
    }

    original_import = _b.__import__
    talib_stub = _build_talib_stub()
    qtpylib_stub = _build_qtpylib_stub()
    # `talib.abstract` is the most common import in Freqtrade strategies
    # (e.g. `import talib.abstract as ta`). Without exposing the same stub
    # under that submodule name, the import fails → user's whole strategy
    # is rejected and the engine falls back to a name-matched built-in
    # with stale DB-default SL/TP. Aliasing the submodule to the same
    # stub object means every `ta.EMA(...)` / `ta.RSI(...)` works.
    talib_stub.abstract = talib_stub
    safe_modules = {
        "freqtrade":              freqtrade_mod,
        "freqtrade.strategy":     freqtrade_mod.strategy,
        "freqtrade.exchange":     freqtrade_mod.exchange,
        "freqtrade.persistence":  freqtrade_mod.persistence,
        "talib":                  talib_stub,
        "talib.abstract":         talib_stub,   # import talib.abstract as ta
        "pandas_ta":              talib_stub,   # close enough for most LLM-emitted code
        "qtpylib":                qtpylib_stub,
        "qtpylib.indicators":     qtpylib_stub.indicators,
        "technical":              types.ModuleType("technical"),
        "technical.indicators":   types.ModuleType("technical.indicators"),
    }

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        # Standard scientific stack: pandas, numpy, math, datetime are fine.
        if name in ("pandas", "numpy", "math", "datetime", "typing",
                    "decimal", "functools", "itertools"):
            return original_import(name, globals, locals, fromlist, level)
        if name in safe_modules:
            return safe_modules[name]
        # Try fall-through to real import for harmless modules. If it's
        # something dangerous (os, subprocess, socket, requests, etc.) we
        # raise so the strategy fails fast and visibly.
        BANNED = {"os", "sys", "subprocess", "socket", "ssl", "requests",
                  "urllib", "urllib2", "urllib3", "httpx", "asyncio",
                  "shutil", "pathlib", "tempfile", "io", "ctypes"}
        if name.split(".")[0] in BANNED:
            raise ImportError(f"Module '{name}' is not available in the strategy sandbox")
        try:
            return original_import(name, globals, locals, fromlist, level)
        except ImportError:
            raise

    allowed["__import__"] = safe_import
    return allowed


def evaluate_strategy(generated_code: str, df: pd.DataFrame) -> pd.DataFrame:
    """Run the user's IStrategy code against `df` and return a copy with
    signal columns added: enter_long, enter_short, exit_long, exit_short.

    Raises RuntimeError with a clear message on any failure so the caller
    can surface it to the user.
    """
    if not generated_code or not generated_code.strip():
        raise RuntimeError("strategy has no generated_code to execute")

    sandbox = _build_sandbox()
    try:
        exec(compile(generated_code, "<user_strategy>", "exec"), sandbox)
    except Exception as e:
        raise RuntimeError(f"Strategy code failed to import: {e}")

    # Find the IStrategy subclass in the sandbox namespace.
    # We walk the MRO and look for ANY ancestor named "IStrategy" rather
    # than strict identity-equality against our stub. The LLM-generated
    # code sometimes imports IStrategy from a slightly different path
    # (freqtrade.strategy.interface, freqtrade.strategy.istrategy, etc.)
    # which would create a different IStrategy object that fails an
    # identity-based issubclass check — even though semantically it's
    # the same Freqtrade interface.
    strategy_cls = None
    user_classes: list[tuple[str, type]] = []
    # Set of class identities we INJECTED into the sandbox (so we can skip
    # them when iterating to find the user's class).
    injected_ids = {id(IStrategy)}
    for name, val in sandbox.items():
        if not isinstance(val, type):
            continue
        if id(val) in injected_ids:
            continue
        # Heuristic: skip anything that came in via the typing module or
        # similar import side-effects. We only care about classes whose
        # module is unknown (created in user code) or whose MRO mentions
        # IStrategy.
        mro_names = [c.__name__ for c in val.__mro__]
        if "IStrategy" in mro_names:
            strategy_cls = val
            break
        # Track non-IStrategy classes too — used as last-resort fallback.
        if getattr(val, "__module__", None) in (None, "__main__", "user_strategy", "<user_strategy>"):
            user_classes.append((name, val))

    # Fallback: if no IStrategy subclass found but exactly one user class
    # is defined, use it. Covers the case where the LLM forgot the explicit
    # subclass or used a different base class name.
    if strategy_cls is None and len(user_classes) == 1:
        strategy_cls = user_classes[0][1]

    if strategy_cls is None:
        defined = [n for n, _ in user_classes] or ["<none>"]
        raise RuntimeError(
            f"strategy code does not define a class subclassing IStrategy. "
            f"Classes found: {', '.join(defined)}"
        )

    try:
        instance = strategy_cls()
    except Exception as e:
        raise RuntimeError(f"Strategy class could not be instantiated: {e}")

    work = df.copy()
    # Freqtrade convention uses `volume`; our KuCoin loader uses `vol`.
    # Alias before the user's code runs, otherwise any reference to
    # dataframe["volume"] raises KeyError → strategy_runner falls back to
    # name-matched built-in → user's edits silently ignored.
    if "vol" in work.columns and "volume" not in work.columns:
        work["volume"] = work["vol"]
    metadata = {"pair": "BTC/USDT"}

    # Diagnostic: log the user-defined methods on their strategy so we can
    # see in Railway logs what entry/exit hooks they actually have. This
    # is invaluable when their class uses non-Freqtrade conventions.
    user_methods = [m for m in dir(instance)
                    if not m.startswith("_") and callable(getattr(instance, m, None))]
    log.info("strategy=%s methods=%s", strategy_cls.__name__, user_methods)

    # Be defensive: not every LLM-emitted strategy inherits from OUR
    # IStrategy stub (the import may resolve to a different object than
    # we provide). In that case populate_* methods won't be inherited and
    # we have to skip the ones the user didn't define on their class.
    def _call_if_exists(method_name: str, what: str):
        nonlocal work
        method = getattr(instance, method_name, None)
        if method is None or not callable(method):
            return  # method not defined on this strategy — skip
        try:
            result = method(work, metadata)
            if isinstance(result, pd.DataFrame):
                work = result
        except Exception as e:
            raise RuntimeError(f"{method_name} failed: {e}")

    # Detect "non-Freqtrade" strategies: classes that don't define any of
    # the populate_* hooks the engine knows how to call. These are usually
    # built-in Python ports (Pine Script translations etc.) authored to
    # run via the name-pattern path (_guess_strategy) — NOT to be exec'd
    # as IStrategy subclasses. Raise so the caller can gracefully fall
    # back to the name-matched signal function.
    HOOKS = ("populate_indicators", "populate_entry_trend",
             "populate_buy_trend",  "populate_exit_trend",
             "populate_sell_trend")
    available = [h for h in HOOKS if callable(getattr(instance, h, None))]
    if not available:
        raise RuntimeError(
            f"Strategy class '{strategy_cls.__name__}' has none of the "
            f"populate_* hooks (populate_indicators / populate_entry_trend / "
            f"populate_buy_trend / populate_exit_trend). This looks like a "
            f"Python-class strategy designed for the built-in signal-pattern "
            f"path — falling back to the name-matched signal function."
        )

    _call_if_exists("populate_indicators", "indicators")
    # Try the new-style entry/exit hooks first, fall back to old buy/sell.
    if hasattr(instance, "populate_entry_trend"):
        _call_if_exists("populate_entry_trend", "entry signals")
    if hasattr(instance, "populate_buy_trend"):
        _call_if_exists("populate_buy_trend", "buy signals (legacy)")
        if "buy" in work.columns and "enter_long" not in work.columns:
            work["enter_long"] = work["buy"]
    if hasattr(instance, "populate_exit_trend"):
        _call_if_exists("populate_exit_trend", "exit signals")
    if hasattr(instance, "populate_sell_trend"):
        _call_if_exists("populate_sell_trend", "sell signals (legacy)")
        if "sell" in work.columns and "exit_long" not in work.columns:
            work["exit_long"] = work["sell"]

    # ── Fallback column-name detection ─────────────────────────────────
    # If the strategy didn't fill enter_long/enter_short directly, look
    # for common Freqtrade-historical column names and map them. Order:
    # newer naming wins.
    _COL_ALIASES = {
        "enter_long":  ["enter_long",  "buy",  "long",  "signal_long",  "go_long",  "entry_long"],
        "enter_short": ["enter_short", "sell_short", "short", "signal_short", "go_short", "entry_short"],
        "exit_long":   ["exit_long",   "sell", "close_long",  "exit_long_signal"],
        "exit_short":  ["exit_short",  "exit_short_signal", "close_short"],
    }
    for target, candidates in _COL_ALIASES.items():
        if target in work.columns and (work[target].fillna(0).astype(int) != 0).any():
            continue   # already populated by user code
        for alt in candidates:
            if alt == target:
                continue
            if alt in work.columns and (work[alt].fillna(0).astype(int) != 0).any():
                # Bool/numeric-friendly copy. Keep both columns alive for
                # downstream debugging, just mirror the values.
                work[target] = work[alt].fillna(0).astype(int)
                break

    # Default any still-missing signal columns to 0 so the engine doesn't KeyError.
    for col in ("enter_long", "enter_short", "exit_long", "exit_short"):
        if col not in work.columns:
            work[col] = 0

    # Diagnostics for the UI: which columns have any non-zero values?
    # When entries fired = 0, the user can see if their strategy populated
    # `buy_signal` instead of `enter_long` etc.
    non_zero_cols = []
    for col in work.columns:
        if col in ("date", "open", "high", "low", "close", "vol"):
            continue
        try:
            if work[col].dtype.kind in "biu" or work[col].dtype == bool:
                if (work[col].fillna(0).astype(int) != 0).any():
                    non_zero_cols.append(col)
            elif work[col].dtype.kind == "f":
                # Floats: include only if it looks like a binary signal
                # (only 0s and 1s) — otherwise it's an indicator value.
                vals = work[col].dropna().unique()
                if len(vals) <= 3 and set(vals).issubset({0.0, 1.0, -1.0}):
                    if (work[col].fillna(0).astype(int) != 0).any():
                        non_zero_cols.append(col)
        except Exception:
            pass

    # Attach a non-data attribute we can inspect in the backtester to
    # surface "which methods did the user define" diagnostics back to UI.
    work.attrs["strategy_methods"] = user_methods
    work.attrs["strategy_class"]   = strategy_cls.__name__
    work.attrs["signal_columns"]   = non_zero_cols

    # Extract the strategy's OWN risk parameters so the backtester can use
    # them instead of stale DB defaults. The Strategy SQLAlchemy model
    # carries column defaults (stoploss=-0.03, take_profit=0.015) that
    # leak through whenever a row was created without explicit values,
    # silently flipping a 1:3 RR strategy into a 1:0.5 RR run that's
    # mathematically guaranteed to lose. The class is the source of truth
    # — but only when its values are SANE. Some strategies use
    # placeholder values like stoploss=-0.99 (a Freqtrade idiom for
    # "no engine-level stop, use custom_stoploss") or minimal_roi={"0": 100}
    # (a placeholder for "ROI handled by custom_exit"). Taking those
    # literally would mean liquidating every trade on the first bar or
    # never taking profit. Sanity-cap: only honour declared values that
    # are inside a realistic retail-trading band.
    SANE_SL_MIN, SANE_SL_MAX = 0.001, 0.25   # 0.1% to 25%
    SANE_TP_MIN, SANE_TP_MAX = 0.001, 0.50   # 0.1% to 50%

    cls_stoploss = getattr(strategy_cls, "stoploss", None)
    if isinstance(cls_stoploss, (int, float)) and cls_stoploss != 0:
        sl_abs = abs(float(cls_stoploss))
        if SANE_SL_MIN <= sl_abs <= SANE_SL_MAX:
            work.attrs["class_stoploss_pct"] = sl_abs * 100
        else:
            work.attrs["class_stoploss_ignored"] = (
                f"{sl_abs*100:.1f}% — outside sane range [{SANE_SL_MIN*100}%–{SANE_SL_MAX*100}%]"
            )

    cls_roi = getattr(strategy_cls, "minimal_roi", None)
    if isinstance(cls_roi, dict) and cls_roi:
        # minimal_roi maps minute-since-entry → required profit fraction.
        # The "0" key is the IMMEDIATE take-profit; use it as the static TP.
        # (Time-decayed ROIs are a Freqtrade concept our engine doesn't
        # honour bar-by-bar — picking the t=0 value is the closest
        # approximation that preserves the strategy author's intent.)
        roi_at_zero = cls_roi.get("0") or cls_roi.get(0)
        if isinstance(roi_at_zero, (int, float)) and roi_at_zero > 0:
            tp_abs = float(roi_at_zero)
            if SANE_TP_MIN <= tp_abs <= SANE_TP_MAX:
                work.attrs["class_take_profit_pct"] = tp_abs * 100
            else:
                work.attrs["class_take_profit_ignored"] = (
                    f"{tp_abs*100:.1f}% — outside sane range [{SANE_TP_MIN*100}%–{SANE_TP_MAX*100}%]"
                )
    return work


def make_signal_fn_from_df(df: pd.DataFrame, leverage: int,
                           stoploss_pct: float, take_profit_pct: float):
    """Adapter: given a dataframe that already has enter_long/enter_short
    columns populated, return a signal_fn(df, i) → (entry_px, sl, tp, dir)
    matching the existing engine's expected shape.

    SL/TP here are the user-defined %s from the backtest UI. The user's
    strategy class may define its own stoploss/minimal_roi but the futures
    engine handles those at the engine level (it doesn't use the strategy's
    custom_stoploss/custom_exit hooks).
    """
    enter_long = df["enter_long"].astype(int).values if "enter_long" in df.columns else None
    enter_short = df["enter_short"].astype(int).values if "enter_short" in df.columns else None

    def signal_fn(_df, i):
        # Edge-only firing (matches TradingView's strategy.entry behaviour):
        # we treat a signal as fired only on the bar where the condition
        # *transitions* from False→True. If the strategy's enter_long stays
        # True for 20 consecutive bars, that's ONE entry signal — not 20.
        # This is what causes "26 signal bars → only 3 trades" confusion:
        # without edge detection, the same setup re-fires every bar while
        # the condition holds, but the engine can only act on the first.
        # With edge detection, the signal count matches the trade count.
        if enter_long is not None and i < len(enter_long) and enter_long[i]:
            prev = enter_long[i - 1] if i > 0 else 0
            if not prev:
                entry = float(_df.iloc[i]["close"])
                sl = entry * (1 - stoploss_pct / 100)
                tp = entry * (1 + take_profit_pct / 100)
                return entry, sl, tp, "long"
        if enter_short is not None and i < len(enter_short) and enter_short[i]:
            prev = enter_short[i - 1] if i > 0 else 0
            if not prev:
                entry = float(_df.iloc[i]["close"])
                sl = entry * (1 + stoploss_pct / 100)
                tp = entry * (1 - take_profit_pct / 100)
                return entry, sl, tp, "short"
        return None

    return signal_fn
