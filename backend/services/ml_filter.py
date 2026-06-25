"""Universal ML loss-filter — a per-strategy 'take/skip' meta-model.

The honest idea (meta-labeling): a rule-based strategy decides DIRECTION; this
model only decides whether to TAKE or SKIP each of ITS signals, learned from the
strategy's own history. It can refine a strategy that already has an edge — it
cannot create one. Every model must pass a walk-forward gate (filtered must beat
unfiltered out-of-sample) before it's used.

Stateless functions here; storage + endpoints + engine wiring live elsewhere.
"""
from __future__ import annotations
import json
import time
import urllib.request
import numpy as np
import pandas as pd
from backend.services import strategy_runner as sr

_GRAN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


def _kc_symbol(pair: str) -> str:
    base = pair.split("/")[0].upper()
    return ("XBT" if base == "BTC" else base) + "USDTM"


def fetch_klines(pair: str, tf: str = "1h", bars: int = 4000) -> pd.DataFrame:
    """Fetch ~`bars` KuCoin futures candles for training (paginated)."""
    gran = _GRAN.get(tf, 60); sym = _kc_symbol(pair); out: list = []; cur = int(time.time() * 1000)
    while len(out) < bars:
        frm = cur - gran * 60_000 * 200
        url = f"https://api-futures.kucoin.com/api/v1/kline/query?symbol={sym}&granularity={gran}&from={frm}&to={cur}"
        d = json.load(urllib.request.urlopen(url, timeout=25)).get("data") or []
        if not d:
            break
        out = d + out; cur = d[0][0] - 1; time.sleep(0.2)
    rows = sorted(out, key=lambda r: r[0])[-bars:]
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "vol", "turn"])
    for c in ["open", "high", "low", "close", "vol"]:
        df[c] = df[c].astype(float)
    df["volume"] = df["vol"]
    df["date"] = pd.to_datetime(df["t"], unit="ms", utc=True)
    return df.reset_index(drop=True)


def deserialize(blob: bytes) -> dict:
    """Load a stored model blob -> {model, features, mu, sd, conf}."""
    import io, joblib
    return joblib.load(io.BytesIO(blob))


def take_mask(model_dict: dict, df: pd.DataFrame) -> np.ndarray:
    """Boolean array (len = len(df)): True = TAKE the signal at that bar,
    False = SKIP (model says low win-probability). Bars with NaN features
    default to TAKE (the model has no opinion)."""
    F = engineer_features(df)
    mu = model_dict["mu"]; sd = model_dict["sd"]; conf = model_dict.get("conf", 0.55)
    X = F.values.astype(float)
    take = np.ones(len(df), dtype=bool)
    valid = ~np.isnan(X).any(axis=1)
    if valid.any():
        p = model_dict["model"].predict_proba((X[valid] - mu) / sd)[:, 1]
        take[np.where(valid)[0]] = p >= conf
    return take


def load_enabled_model(db, user_id: str, strategy_id, require_enabled: bool = True) -> dict | None:
    """Return the deserialized PASSing model for (user, strategy), or None.
    require_enabled=True (live engine) needs the user to have toggled it on;
    False (backtest preview) loads the latest passing model regardless. Fail-safe."""
    if not strategy_id:
        return None
    try:
        from sqlalchemy import select
        from backend.models.trade import MLFilterModel
        q = select(MLFilterModel.model_blob, MLFilterModel.conf).where(
            MLFilterModel.user_id == user_id,
            MLFilterModel.strategy_id == strategy_id,
            MLFilterModel.verdict == "PASS",
        )
        if require_enabled:
            q = q.where(MLFilterModel.enabled == True)  # noqa: E712
        row = db.execute(q.order_by(MLFilterModel.created_at.desc()).limit(1)).first()
        if not row or not row[0]:
            return None
        md = deserialize(row[0])
        if row[1]:
            md["conf"] = float(row[1])
        return md
    except Exception:
        return None


def train_strategy_filter(generated_code: str, pairs: list[str], timeframe: str = "1h",
                          bars: int = 4000, conf: float = 0.55) -> dict:
    """Full pipeline: fetch each pair, run the strategy, pool its signals, train +
    walk-forward gate. Returns the report (with model_bytes if it PASSES)."""
    from backend.services.strategy_runner import evaluate_strategy
    rows: list = []
    for pair in pairs:
        try:
            df = fetch_klines(pair, timeframe, bars)
            sig = evaluate_strategy(generated_code, df, pair=pair, execution_tf=timeframe)
            rows += build_dataset(df, sig)
        except Exception:
            continue
    res = train_and_gate(rows, conf=conf)
    res["context"] = f"{', '.join(p.split('/')[0] for p in pairs)} · {timeframe}"
    return res

FEATURE_COLS = [
    "rsi", "willr", "cmo", "adx", "pdi", "mdi", "cci", "natr", "macdh",
    "emaspread", "roc5", "roc10", "mfi", "stochrsi", "bbpos", "ema50dist",
    "ret1", "ret3", "ret5", "ret10", "volratio",
]


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    """Stationary features at every bar (never raw price)."""
    ta = sr._build_talib_stub()
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    f = pd.DataFrame(index=df.index)
    f["rsi"] = ta.RSI(c, 14); f["willr"] = ta.WILLR(h, l, c, 14); f["cmo"] = ta.CMO(c, 14)
    f["adx"] = ta.ADX(h, l, c, 14); f["pdi"] = ta.PLUS_DI(h, l, c, 14); f["mdi"] = ta.MINUS_DI(h, l, c, 14)
    f["cci"] = ta.CCI(h, l, c, 20); f["natr"] = ta.NATR(h, l, c, 14); f["macdh"] = ta.MACD(c)["macdhist"]
    f["emaspread"] = ta.EMA_SPREAD(c); f["roc5"] = ta.ROC(c, 5); f["roc10"] = ta.ROC(c, 10)
    f["mfi"] = ta.MFI(df)
    k, _ = ta.STOCHRSI(c); f["stochrsi"] = k
    bb = ta.BBANDS(c, 20)
    f["bbpos"] = (c - bb["lowerband"]) / (bb["upperband"] - bb["lowerband"]).replace(0, 1e-9)
    f["ema50dist"] = (c - ta.EMA(c, 50)) / c
    for k_ in (1, 3, 5, 10):
        f[f"ret{k_}"] = c.pct_change(k_)
    f["volratio"] = v / v.rolling(20).mean().replace(0, 1e-9)
    return f[FEATURE_COLS]


def build_dataset(df: pd.DataFrame, sig: pd.DataFrame, max_hold: int = 48,
                  fee: float = 0.0008) -> list[tuple]:
    """For each of the strategy's signals, snapshot features + simulate the
    outcome (TP-before-SL = win). Returns rows: (bar_idx, features[list], win, ret)."""
    F = engineer_features(df)
    idx = df.index
    el = sig["enter_long"].fillna(0).astype(int).values if "enter_long" in sig else np.zeros(len(df))
    es = sig["enter_short"].fillna(0).astype(int).values if "enter_short" in sig else np.zeros(len(df))
    slp = sig["sl_price"].values if "sl_price" in sig else np.full(len(df), np.nan)
    tpp = sig["tp_price"].values if "tp_price" in sig else np.full(len(df), np.nan)
    epx = sig["entry_price"].values if "entry_price" in sig else np.full(len(df), np.nan)
    c = df["close"].values; hi = df["high"].values; lo = df["low"].values
    n = len(df); rows = []
    for i in range(1, n - 1):
        if not ((el[i] and not el[i - 1]) or (es[i] and not es[i - 1])):
            continue
        d = "long" if (el[i] and not el[i - 1]) else "short"
        entry = epx[i] if not np.isnan(epx[i]) else c[i]
        sl = slp[i] if not np.isnan(slp[i]) else entry * (0.98 if d == "long" else 1.02)
        tp = tpp[i] if not np.isnan(tpp[i]) else entry * (1.04 if d == "long" else 0.96)
        win = None
        for j in range(i + 1, min(n, i + max_hold + 1)):
            if d == "long":
                if lo[j] <= sl: win = 0; break
                if hi[j] >= tp: win = 1; break
            else:
                if hi[j] >= sl: win = 0; break
                if lo[j] <= tp: win = 1; break
        if win is None:
            end = min(n - 1, i + max_hold)
            win = 1 if ((c[end] > entry) == (d == "long")) else 0
        ret = (abs(tp - entry) / entry if win else -abs(entry - sl) / entry) - fee
        feat = F.iloc[i]
        if feat.isna().any():
            continue
        rows.append((i, [float(x) for x in feat.values], int(win), float(ret)))
    return rows


def train_and_gate(rows: list[tuple], n_windows: int = 4, conf: float = 0.55) -> dict:
    """Walk-forward: train on past signals, test on future. Compare UNFILTERED
    (take all) vs FILTERED (take only P(win) > conf). Returns the verdict + the
    full-data model bytes IF it passes the gate (filtered beats unfiltered in a
    majority of windows AND on aggregate)."""
    try:
        import lightgbm as lgb
    except Exception as e:
        return {"ok": False, "reason": f"ML engine not ready on the server ({e}). "
                "A redeploy with libgomp1 installed is needed — try again after it deploys."}
    if len(rows) < 120:
        return {"ok": False, "reason": f"too few signals to train ({len(rows)}); need ~120+. "
                "Use more pairs, a longer range, or a higher-frequency strategy."}
    X = np.array([r[1] for r in rows]); y = np.array([r[2] for r in rows]); ret = np.array([r[3] for r in rows])
    n = len(X)
    base_wr = y.mean()

    def fit(a, b):
        m = lgb.LGBMClassifier(n_estimators=250, max_depth=3, learning_rate=0.03,
                               subsample=0.8, colsample_bytree=0.8, min_child_samples=30,
                               reg_lambda=1.0, verbose=-1)
        m.fit(a, b); return m

    wins_unf, wins_fil, wf = 0, 0, []
    for k in range(n_windows):
        tr_end = int(n * (0.4 + 0.15 * k)); te_end = int(n * (0.55 + 0.15 * k))
        if te_end - tr_end < 20:
            continue
        mu = X[:tr_end].mean(0); sd = X[:tr_end].std(0) + 1e-9
        m = fit((X[:tr_end] - mu) / sd, y[:tr_end])
        p = m.predict_proba((X[tr_end:te_end] - mu) / sd)[:, 1]
        rte = ret[tr_end:te_end]
        unf = rte.sum() * 100
        keep = p > conf
        fil = rte[keep].sum() * 100
        wf.append({"window": k + 1, "test_signals": int(te_end - tr_end),
                   "kept": int(keep.sum()), "unfiltered_net": round(unf, 2),
                   "filtered_net": round(fil, 2), "improvement": round(fil - unf, 2)})
        wins_unf += unf; wins_fil += fil
    better = sum(1 for w in wf if w["improvement"] > 0)
    passed = bool(wf) and better >= max(1, round(len(wf) * 0.6)) and wins_fil > wins_unf
    out = {"ok": True, "signals": n, "base_win_rate": round(base_wr * 100, 1),
           "windows": wf, "windows_better": better, "windows_total": len(wf),
           "agg_unfiltered_net": round(wins_unf, 2), "agg_filtered_net": round(wins_fil, 2),
           "verdict": "PASS" if passed else "FAIL", "conf": conf}
    if passed:
        import joblib, io
        mu = X.mean(0); sd = X.std(0) + 1e-9
        full = lgb.LGBMClassifier(n_estimators=250, max_depth=3, learning_rate=0.03,
                                  subsample=0.8, colsample_bytree=0.8, min_child_samples=30,
                                  reg_lambda=1.0, verbose=-1)
        full.fit((X - mu) / sd, y)
        buf = io.BytesIO()
        joblib.dump({"model": full, "features": FEATURE_COLS, "mu": mu, "sd": sd, "conf": conf}, buf)
        out["model_bytes"] = buf.getvalue()
    return out
