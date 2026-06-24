"""
Futures trading endpoints — paper and live with leverage.
Completely isolated from spot trading (different market_type='futures').

Includes: order book, recent trades, manual order placement, leverage/margin control,
pending orders, positions, bot management, and account overview.
"""
from __future__ import annotations

import logging
import time as _time
from datetime import datetime
from fastapi import APIRouter, Body, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select, desc, func

from backend.models import get_db
from backend.models.trade import Trade, StrategyInstance, FuturesOrder
from backend.models.config import Config
from backend.utils.clerk_auth import get_user_id
from backend.services.futures_engine import futures_engine_registry
from backend.utils.audit import log_event

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/futures", tags=["futures"])

# Lead Futures Trading: max leverage allowed by KuCoin lead trading account
LEAD_MAX_LEVERAGE = 20

# Simple in-memory cache for order book / trades (avoid hammering KuCoin)
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 1.5  # seconds


# Per-pair price cache. /open + /account + force-close + partial-close all
# call _futures_ticker_price; without a cache, a Positions tab with 5
# pairs makes 5 SEQUENTIAL KuCoin calls (each up to 5s timeout) on every
# 8s poll — the user-visible "futures terminal is slow" symptom. 2s TTL
# is short enough that prices stay essentially real-time (5x faster than
# the UI poll interval) but long enough that all 6 endpoints in a single
# poll cycle hit the cache after the first miss.
_ticker_cache: dict[str, tuple[float, float]] = {}   # pair → (timestamp, price)
_TICKER_TTL = 2.0


# Recently-closed live positions guard. KuCoin's /api/v1/positions is
# eventually consistent — after we send a successful close, KuCoin may
# still report the position as open for a few seconds. Without this
# guard, /open's reconcile would RE-ADD the position to the user's
# Positions tab right after they closed it, making the close appear
# to fail. We record (user_id, pair, direction) → close_ts when a
# close confirms, and the reconcile skips any KuCoin-only row that
# matches a recent close.
_recently_closed: dict[tuple[str, str, str], float] = {}
_RECENTLY_CLOSED_TTL = 30.0   # seconds — covers KuCoin's worst-case
                              # post-close consistency window


def _mark_recently_closed(user_id: str, pair: str, direction: str) -> None:
    """Record that we just closed (user_id, pair, direction) so /open
    skips KuCoin's stale 'still open' echo for a few seconds."""
    import time as _t
    _recently_closed[(user_id, pair, direction)] = _t.time()


def _was_recently_closed(user_id: str, pair: str, direction: str) -> bool:
    """Check if (user_id, pair, direction) was closed in the last
    _RECENTLY_CLOSED_TTL seconds. Also opportunistically prunes
    expired entries to keep the dict from growing."""
    import time as _t
    now = _t.time()
    key = (user_id, pair, direction)
    ts = _recently_closed.get(key)
    if ts is None:
        return False
    if now - ts > _RECENTLY_CLOSED_TTL:
        _recently_closed.pop(key, None)
        return False
    return True


# ── Stale LIVE-position reconcile (bugs 1 & 3) ───────────────────────────────
# A LIVE row shown in Positions can be stale: closed on KuCoin's own UI, or
# its owning engine was lost on a backend restart so nothing flipped the DB
# row from "open" to "closed". KuCoin's /api/v1/positions LIST is the source
# of truth — a LIVE (pair, direction) NOT present there (currentQty != 0) is
# closed. BUT that endpoint is eventually consistent and has a documented
# transient "returns currentQty=0 for a real position" quirk, so a single
# absence is not proof. We require N CONSECUTIVE misses (across /open polls)
# before dropping a row + closing its DB record. A row confirmed present on
# KuCoin resets its counter. A freshly-opened position (within the grace
# window) is never counted as missing — KuCoin may not have propagated the
# open yet.
_live_miss: dict[tuple[str, str, str], int] = {}
_LIVE_MISS_DROP_THRESHOLD = 3    # consecutive /open polls confirming "gone"
_LIVE_FRESH_GRACE_SECS = 20.0    # never reconcile-drop a position younger than this


def _futures_ticker_price(pair: str) -> float | None:
    """Bug-fix helper: get the FUTURES perp last price for a pair.

    Before this, six different endpoints in this router were calling
    `_kucoin_get('/api/v1/market/orderbook/level1', ...)` which hits the
    SPOT API (api.kucoin.com). Spot/perp basis drift means the P&L on
    Positions, the force-close exit price, and the manual-order panel
    indicators were all using slightly wrong reference prices. This
    helper hits api-futures.kucoin.com so every futures-router endpoint
    references the correct market.

    Cached for _TICKER_TTL seconds to collapse parallel calls within a
    single UI poll cycle into one upstream request per pair.

    Returns None on transport / parsing failure so the caller can fall
    back to an entry-price-based P&L estimate."""
    import time as _t
    now = _t.time()
    cached = _ticker_cache.get(pair)
    if cached is not None and (now - cached[0]) < _TICKER_TTL:
        return cached[1]
    try:
        from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
        from backend.services.kucoin_futures_client import (
            normalize_futures_symbol, KUCOIN_FUTURES_BASE as _base,
        )
        import urllib.request as _ureq, json as _json
        sym = normalize_futures_symbol(
            pair.replace("/", "").replace("USDT", "USDTM"),
        )
        url = f"{_base}/api/v1/ticker?symbol={sym}"
        req = _ureq.Request(url, headers={"User-Agent": "AutoTradeHub/2.0"})
        # Tightened from 5s to 2s — the UI poll runs 6 endpoints in
        # parallel; one slow ticker request stalling at 5s blocked the
        # whole Positions tab refresh. 2s is more than enough for
        # KuCoin's healthy p95 (~150ms) while failing fast on outages.
        with _proxy_urlopen(req, timeout=2) as resp:
            data = _json.loads(resp.read().decode())
        if str(data.get("code")) != "200000":
            return None
        d = data.get("data") or {}
        p = float(d.get("price", 0) or 0)
        if p > 0:
            _ticker_cache[pair] = (now, p)
            return p
        return None
    except Exception:
        return None


# ── KuCoin Futures lot-size table ────────────────────────────────────────────
# KuCoin contracts have a fixed multiplier — the amount of underlying per
# contract. Smallest order is 1 contract, so 1 contract's *notional* sets the
# minimum order size at a given price/leverage.
# Values cross-checked against KuCoin API /api/v1/contracts/active. Add more
# here if users start trading new pairs; unknown symbols fall back to 0.001.
_LOT_SIZE_BY_SYMBOL: dict[str, float] = {
    "XBTUSDTM":  0.001,
    "XAUTUSDTM": 0.001,   # Tether Gold (gold-backed token perp)
    "PAXGUSDTM": 0.001,   # PAX Gold (gold-backed token perp)
    "ETHUSDTM":  0.01,
    "SOLUSDTM":  0.1,
    "XRPUSDTM":  10.0,
    "DOGEUSDTM": 1000.0,
    "ADAUSDTM":  10.0,
    "AVAXUSDTM": 0.1,
    "BNBUSDTM":  0.01,
    "LTCUSDTM":  0.1,
    "LINKUSDTM": 1.0,
    "MATICUSDTM": 10.0,
    "DOTUSDTM":  1.0,
    "TRXUSDTM":  100.0,
    "ATOMUSDTM": 1.0,
    "OPUSDTM":   1.0,
    "ARBUSDTM":  1.0,
}


def _futures_lot_size(kc_symbol: str) -> float:
    """Return the contract multiplier (underlying-per-lot) for a KuCoin futures
    symbol.

    Fast path: a hardcoded table for the common pairs (no network call).

    Unknown symbols (anything outside the static table — SUI, PEPE, WIF, …)
    fall back to a ONE-TIME KuCoin /contracts lookup (cached in
    futures_engine._CONTRACT_MULTIPLIERS) and the result is memoised here so
    the next call is instant. Previously these silently used BTC's 0.001
    multiplier, which over- or under-sized the order — KuCoin then rejected
    or clamped it, surfacing to the user as a slow/failed entry or close on
    every non-listed coin. Resolving the real multiplier makes entry AND
    close exact and consistent for every supported coin.

    Total-failure fallback is still 0.001 (BTC) — better a conservative
    guess than a crash; KuCoin's rejection will still surface if it's wrong.
    """
    key = kc_symbol.upper()
    val = _LOT_SIZE_BY_SYMBOL.get(key)
    if val is not None:
        return val
    try:
        from backend.services.futures_engine import _get_contract_multiplier
        mult, _lot = _get_contract_multiplier(key)
        if mult and mult > 0:
            _LOT_SIZE_BY_SYMBOL[key] = mult   # memoise for the next call
            return mult
    except Exception as e:
        log.warning("dynamic lot-size lookup failed for %s (using BTC default): %s", key, e)
    return 0.001


def _compute_live_sizing(cost_usdt: float, leverage: int, price: float,
                          kc_symbol: str) -> tuple[int | None, float, float, str | None]:
    """
    Convert a user-typed `cost_usdt` (USDT they want to commit as margin) into
    an exact KuCoin contract count, plus the *real* margin and notional that
    will actually be locked.

    Returns: (contracts, real_margin_usdt, real_notional_usdt, error_message)

      contracts          — integer lot count to send to KuCoin (None on error).
      real_margin_usdt   — what KuCoin will actually lock from your wallet.
      real_notional_usdt — position value at entry (margin * leverage).
      error_message      — non-empty when the order would be rejected; the
                            caller should return this to the frontend BEFORE
                            mutating engine state so we don't leave phantoms.

    Why this exists: the old code did `max(1, int(notional / price * 1000))`
    which silently rounded a $1 BTC order at 1x up to 1 contract (0.001 BTC ≈
    $79). Users got 80× the exposure they asked for. Now we compute the
    minimum cost for 1 contract at the chosen leverage and reject below it.
    """
    if leverage <= 0:
        return None, 0.0, 0.0, "Leverage must be ≥ 1."
    if price <= 0:
        return None, 0.0, 0.0, "Could not fetch a valid price for this symbol."
    if cost_usdt <= 0:
        return None, 0.0, 0.0, "Enter a cost greater than 0."

    lot = _futures_lot_size(kc_symbol)
    # Margin required for 1 contract at this leverage:
    #   notional_per_lot = lot * price
    #   margin_per_lot   = notional_per_lot / leverage
    min_margin = (lot * price) / leverage
    if cost_usdt < min_margin:
        return None, 0.0, 0.0, (
            f"Minimum cost for {kc_symbol} at {leverage}x leverage is "
            f"{min_margin:.2f} USDT (1 contract = {lot} {kc_symbol[:-5]} ≈ "
            f"{lot * price:.2f} USDT notional). Increase the Cost or leverage."
        )

    # User can afford ≥1 contract — pick the largest lot count whose margin
    # fits within their cost. round() would over-fill; floor is the right
    # consumer-friendly choice (charges no more than asked).
    notional_target = cost_usdt * leverage
    contracts = int(notional_target / (lot * price))
    contracts = max(1, contracts)  # safety; min_margin guard above guarantees ≥1
    real_notional = contracts * lot * price
    real_margin = real_notional / leverage
    return contracts, round(real_margin, 4), round(real_notional, 4), None


# Per-(user, symbol) cache of the last leverage + margin_mode we
# successfully pushed to KuCoin. Each live entry was doing two extra
# round trips (changeMarginMode + changeLeverage) on EVERY trade even
# when the values hadn't changed — that's ~600ms of pure overhead per
# click. Once we've synced, we know KuCoin's state until either the
# user changes the setting in the UI or an order rejection forces a
# re-sync. Cleared on a "margin mode mismatch" rejection (KuCoin tells
# us the cache is stale and we re-sync on the next attempt).
_kc_settings_cache: dict[tuple[str, str], dict] = {}
# {(user_id, kc_symbol): {"leverage": int, "margin_mode": "CROSS"|"ISOLATED"}}


def _invalidate_kc_settings_cache(user_id: str, kc_symbol: str | None = None) -> None:
    """Drop cached leverage/margin_mode after a stale-related rejection
    so the next call re-syncs. If kc_symbol is None, drop all entries
    for the user (used when API keys are rotated)."""
    if kc_symbol is None:
        keys = [k for k in _kc_settings_cache if k[0] == user_id]
        for k in keys:
            _kc_settings_cache.pop(k, None)
    else:
        _kc_settings_cache.pop((user_id, kc_symbol), None)


def _sync_margin_mode_to_kucoin(eng, kc_symbol: str, desired_mode: str,
                                  user_id: str) -> tuple[bool, str | None]:
    """
    Push the user's chosen Cross/Isolated setting to KuCoin Futures BEFORE
    placing an order — so the order body's `marginMode` field matches the
    symbol's configured mode on the exchange.

    Without this, the toggle in the UI only updates engine local memory.
    KuCoin still has the previous mode, and the next order is rejected with
    "The order's margin mode does not match the selected one".

    Caches the last successful sync per (user, symbol) so back-to-back
    entries with the same margin mode skip the changeMarginMode call —
    typically saves ~300ms per trade.

    Returns (ok, error_message).
      - ok=True even if KuCoin is already in `desired_mode` (idempotent).
      - ok=False with a user-facing message if KuCoin refuses (most common
        reason: an open position on the symbol is locking the mode).
    """
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE

    mode_upper = (desired_mode or "ISOLATED").upper()
    if mode_upper not in ("CROSS", "ISOLATED"):
        return False, f"Invalid margin mode: {desired_mode}"

    # Fast path: cache says KuCoin is already in this mode. Skip the call.
    cache_key = (user_id, kc_symbol)
    cached = _kc_settings_cache.get(cache_key)
    if cached and cached.get("margin_mode") == mode_upper:
        return True, None

    try:
        resp = _kucoin_post_signed(
            "/api/v2/position/changeMarginMode",
            {"symbol": kc_symbol, "marginMode": mode_upper},
            eng._api_key, eng._api_sec, eng._api_pass,
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("[%s] changeMarginMode network error for %s: %s",
                    user_id, kc_symbol, e)
        # Don't block the order on a network blip — let the actual order
        # attempt either succeed or surface the real error.
        return True, None

    code = str(resp.get("code", ""))
    if code == "200000":
        log.info("[%s] Synced %s margin mode to %s on KuCoin",
                 user_id, kc_symbol, mode_upper)
        _kc_settings_cache.setdefault(cache_key, {})["margin_mode"] = mode_upper
        return True, None

    msg = (resp.get("msg") or "").lower()
    # 330005 = already in this mode (older API); treat as success.
    # Some accounts return 200000 with msg "already set" — also fine.
    if "already" in msg or code in ("330005", "330006"):
        _kc_settings_cache.setdefault(cache_key, {})["margin_mode"] = mode_upper
        return True, None

    # KuCoin's typical block: "Please close all open positions first" or
    # "The margin mode cannot be modified". Surface it clearly.
    log.warning("[%s] changeMarginMode rejected: code=%s msg=%s",
                user_id, code, resp.get("msg"))
    return False, (
        f"Could not switch {kc_symbol} to {mode_upper} margin mode on KuCoin: "
        f"{resp.get('msg', 'rejected')}. Close any open positions on this "
        f"symbol first, or pick the margin mode that matches your existing "
        f"position."
    )


def _sync_leverage_to_kucoin(eng, kc_symbol: str, leverage: int,
                              user_id: str) -> None:
    """Push the user's chosen leverage to KuCoin (best-effort, idempotent).

    Failures are logged but never block the order — the leverage is also
    in the order body itself; this just keeps the per-symbol setting on
    KuCoin in sync so other tooling sees the same number.

    Cached per (user, symbol) — skip the call when the cached value
    matches. Saves ~300ms per trade for back-to-back entries at the
    same leverage on the same pair.
    """
    cache_key = (user_id, kc_symbol)
    cached = _kc_settings_cache.get(cache_key)
    if cached and int(cached.get("leverage") or 0) == int(leverage):
        return  # already in sync

    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    try:
        _kucoin_post_signed(
            "/api/v2/position/changeLeverage",
            {"symbol": kc_symbol, "leverage": str(leverage)},
            eng._api_key, eng._api_sec, eng._api_pass,
            base_url=KUCOIN_FUTURES_BASE,
        )
        _kc_settings_cache.setdefault(cache_key, {})["leverage"] = int(leverage)
    except Exception as e:
        log.warning("[%s] changeLeverage failed for %s lev=%s: %s",
                    user_id, kc_symbol, leverage, e)


def _fetch_kucoin_symbol_settings(eng, kc_symbol: str,
                                    user_id: str) -> tuple[int | None, str | None]:
    """
    Read KuCoin's current per-symbol leverage and margin mode.

    Why: KuCoin's Cross mode keeps one shared leverage per symbol that
    doesn't always match what we send in the order body. The app's UI
    needs to show that REAL value (e.g. "Cross 3.00x"), not the requested
    one, so the user isn't surprised when their $1 margin actually leverages
    a $79 position.

    Returns (leverage:int | None, margin_mode:"CROSS"/"ISOLATED" | None).
    Either may be None if KuCoin doesn't respond or has no record for the
    symbol — the caller falls back to engine state in that case.
    """
    from backend.services.native_trading_engine import _kucoin_get_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE

    try:
        resp = _kucoin_get_signed(
            "/api/v1/position",
            eng._api_key, eng._api_sec, eng._api_pass,
            params={"symbol": kc_symbol},
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("[%s] fetch position settings failed for %s: %s",
                    user_id, kc_symbol, e)
        return None, None

    if str(resp.get("code")) != "200000":
        return None, None

    data = resp.get("data") or {}
    # `realLeverage` is the live effective leverage; `leverage` is the per-symbol
    # configured value. Prefer realLeverage when a position is open (matches
    # what KuCoin's UI shows), fall back to the configured leverage otherwise.
    real_lev = data.get("realLeverage")
    cfg_lev  = data.get("leverage")
    try:
        lev_val = float(real_lev) if real_lev not in (None, 0, "0") else float(cfg_lev or 0)
        lev_int = int(round(lev_val)) if lev_val else None
    except (TypeError, ValueError):
        lev_int = None

    # marginMode field in /api/v1/position payload is one of "ISOLATED"/"CROSS".
    # Older API returned crossMode boolean instead; handle both.
    mode_raw = (data.get("marginMode") or "").upper()
    if not mode_raw:
        cross_flag = data.get("crossMode")
        if cross_flag is True:
            mode_raw = "CROSS"
        elif cross_flag is False:
            mode_raw = "ISOLATED"
    return lev_int, (mode_raw or None)


def _ensure_live_credentials(eng, user_id: str, db: Session) -> tuple[bool, str | None]:
    """
    Make sure the futures engine has the user's KuCoin Lead Trading credentials
    loaded — even when no bot was explicitly started in live mode.

    Manual market / limit orders and force-closes call this before talking to
    the Lead Trading REST API. Without it, `eng._api_key` is "" (engine still
    in its default paper state) and the live REST call is silently skipped,
    leaving a phantom position in the UI with nothing on KuCoin.

    Returns: (ok, error_message).
        ok=True  → eng._api_key / _api_sec / _api_pass are populated.
        ok=False → keys missing or undecryptable; error_message is user-facing.
    """
    from backend.utils.encryption import decrypt, DecryptError

    if eng._api_key and eng._api_sec and eng._api_pass:
        return True, None  # already loaded (e.g. live bot is running)

    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not cfg or not (cfg.kucoin_key_enc and cfg.kucoin_secret_enc and cfg.kucoin_passphrase_enc):
        return False, ("KuCoin API key not configured. Go to Setup → add a Lead-Trading "
                       "futures API key (General + Trade permissions, no Withdraw).")
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return False, "Could not decrypt KuCoin credentials. Re-enter them in Setup."

    if not (kk and ks and kp):
        return False, "KuCoin credentials are blank. Re-enter them in Setup."

    eng._api_key  = kk
    eng._api_sec  = ks
    eng._api_pass = kp
    log.info("[%s] Loaded KuCoin Lead Trading credentials into futures engine on demand.", user_id)
    return True, None


def _decode_strategy_flags(raw):
    """Decode a StrategyInstance.strategy_flags JSON column into a flat dict of
    booleans (or None). Tolerates NULL / legacy / malformed values so an old
    row can never crash auto-resume."""
    if not raw:
        return None
    if isinstance(raw, dict):
        d = raw
    else:
        try:
            import json as _json
            d = _json.loads(raw)
        except Exception:
            return None
    if not isinstance(d, dict) or not d:
        return None
    out = {}
    for k, v in d.items():
        if isinstance(v, bool):
            out[str(k)] = v
        elif isinstance(v, (int, float)):
            out[str(k)] = v
    return out or None


def _fetch_kucoin_live_position(eng, pair: str, direction: str | None = None) -> dict | None:
    """Fetch ONE open live position straight from KuCoin by (pair, direction).

    The futures terminal surfaces real-money positions directly from
    KuCoin's /positions in /open (rows tagged 'kucoin-…') even when no
    engine or Trade row tracks them — engine state lost on a Railway
    restart, a position opened in KuCoin's own UI, or a filled limit order.
    Add-margin and partial-close MUST be able to act on those positions too,
    otherwise the user can see the position but every action errors
    'No open position'.

    `eng` must already have live credentials loaded (call
    _ensure_live_credentials first). Returns a normalised dict
    (kc_symbol, direction, entry, margin, leverage, liq, contracts,
    margin_mode) or None when KuCoin has no matching OPEN position.
    """
    try:
        from backend.services.native_trading_engine import _kucoin_get_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        resp = _kucoin_get_signed(
            "/api/v1/positions",
            eng._api_key, eng._api_sec, eng._api_pass,
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("KuCoin /positions fetch failed for %s: %s", pair, e)
        return None
    if str((resp or {}).get("code")) != "200000":
        return None

    def _dir(kp: dict) -> str | None:
        q = float(kp.get("currentQty", 0) or 0)
        if q == 0:
            return None
        s = str(kp.get("positionSide", "") or "").upper()
        if s == "LONG":
            return "long"
        if s == "SHORT":
            return "short"
        return "long" if q > 0 else "short"

    want = pair.upper()
    for kp in (resp.get("data") or []):
        d = _dir(kp)
        if d is None:
            continue
        ksym = kp.get("symbol", "")
        kbase = ksym.replace("USDTM", "").replace("XBT", "BTC")
        if f"{kbase}/USDT".upper() != want:
            continue
        if direction is not None and d != direction:
            continue
        # Margin mode: newer API returns marginMode "ISOLATED"/"CROSS";
        # older returns a crossMode boolean. Handle both.
        mm = str(kp.get("marginMode", "") or "").upper()
        if mm not in ("ISOLATED", "CROSS"):
            cross = kp.get("crossMode")
            mm = "CROSS" if cross is True else "ISOLATED" if cross is False else ""
        return {
            "kc_symbol":   ksym,
            "direction":   d,
            "entry":       float(kp.get("avgEntryPrice", 0) or 0),
            "margin":      float(kp.get("posMargin", 0) or kp.get("maintMargin", 0) or 0),
            "leverage":    float(kp.get("realLeverage", 0) or kp.get("leverage", 1) or 1),
            "liq":         (float(kp.get("liquidationPrice", 0) or 0) or None),
            "contracts":   abs(float(kp.get("currentQty", 0) or 0)),
            "margin_mode": (mm.lower() if mm else None),
        }
    return None


def _kucoin_deposit_margin(user_id: str, db: Session, pair: str,
                           amount: float) -> tuple[bool, object]:
    """POST KuCoin Futures deposit-margin for `amount` USDT on `pair`.

    Single source of truth for the deposit-margin REST call, shared by the
    engine/DB-orphan add-margin path and the KuCoin-only fallback. Returns
    (True, kucoin_data_dict) on success or (False, error_message) on any
    failure (missing creds, non-200000 business code, transport error)."""
    try:
        from backend.services.native_trading_engine import _kucoin_post_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        from backend.services.kucoin_futures_client import normalize_futures_symbol
        from backend.utils.encryption import decrypt
        cfg = db.execute(
            select(Config).where(Config.user_id == user_id).limit(1)
        ).scalar_one_or_none()
        if not cfg:
            return False, "No KuCoin credentials configured"
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
        if not (kk and ks and kp):
            return False, "KuCoin credentials missing"
        # `pair` is "BASE/USDT" — full futures-symbol transform (XBT remap).
        sym = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        biz_no = f"add-margin-{user_id[-8:]}-{int(_time.time() * 1000)}"
        result = _kucoin_post_signed(
            "/api/v1/position/margin/deposit-margin",
            {"symbol": sym, "margin": amount, "bizNo": biz_no},
            kk, ks, kp,
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(result.get("code")) != "200000":
            return False, f"KuCoin rejected: {result.get('msg', 'unknown')}"
        return True, result.get("data")
    except Exception as exc:
        return False, f"KuCoin margin deposit failed: {exc}"


# ── Live Verified Results Dashboard ──────────────────────────────────────────

@router.get("/dashboard")
def futures_dashboard(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Live results — REAL closed-trade P&L per bot (paper + live), NOT backtests.
    Returns today's P&L, per-bot stats, recent history, and an equity curve.
    This is the 'verifiable results' view: actual trading only."""
    from datetime import datetime, timezone
    from backend.models.trade import Trade
    from backend.models.strategy import Strategy

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = db.execute(
        select(Trade.strategy_id, Trade.mode, Trade.pair, Trade.profit_abs,
               Trade.profit_pct, Trade.exit_time, Trade.side, Trade.exit_reason)
        .where(Trade.user_id == user_id, Trade.status == "closed",
               Trade.market_type == "futures")
        .order_by(Trade.exit_time.desc().nullslast(), Trade.id.desc())
        .limit(2000)
    ).all()

    names: dict[int, str] = {}
    sids = {r.strategy_id for r in rows if r.strategy_id}
    if sids:
        for sid, nm in db.execute(
            select(Strategy.id, Strategy.name).where(Strategy.id.in_(sids))
        ).all():
            names[sid] = nm

    def _is_today(et):
        if not et:
            return False
        et = et if et.tzinfo else et.replace(tzinfo=timezone.utc)
        return et >= today_start

    per_bot: dict = {}
    today_pnl = {"paper": 0.0, "live": 0.0}
    total_pnl = {"paper": 0.0, "live": 0.0}
    for r in rows:
        mode = r.mode if r.mode in ("paper", "live") else "paper"
        pnl = float(r.profit_abs or 0.0)
        is_today = _is_today(r.exit_time)
        total_pnl[mode] += pnl
        if is_today:
            today_pnl[mode] += pnl
        b = per_bot.setdefault((r.strategy_id, mode), {
            "strategy": names.get(r.strategy_id, "Manual / unknown"),
            "mode": mode, "trades": 0, "wins": 0, "total_pnl": 0.0, "today_pnl": 0.0})
        b["trades"] += 1
        b["wins"] += 1 if pnl > 0 else 0
        b["total_pnl"] += pnl
        if is_today:
            b["today_pnl"] += pnl

    bots = []
    for b in per_bot.values():
        bots.append({**b,
                     "win_rate": round(b["wins"] / b["trades"] * 100, 1) if b["trades"] else 0.0,
                     "total_pnl": round(b["total_pnl"], 2),
                     "today_pnl": round(b["today_pnl"], 2)})
    bots.sort(key=lambda x: x["total_pnl"], reverse=True)

    chrono = sorted([r for r in rows if r.exit_time], key=lambda r: r.exit_time)
    eq = []
    cum = 0.0
    for r in chrono[-200:]:
        cum += float(r.profit_abs or 0.0)
        eq.append({"t": r.exit_time.isoformat(), "pnl": round(cum, 2)})

    history = [{
        "strategy": names.get(r.strategy_id, "—"), "mode": r.mode, "pair": r.pair,
        "side": r.side, "profit_abs": round(float(r.profit_abs or 0), 2),
        "profit_pct": round(float(r.profit_pct or 0), 2),
        "exit_reason": r.exit_reason,
        "exit_time": r.exit_time.isoformat() if r.exit_time else None,
    } for r in rows[:50]]

    return {
        "today_pnl": {k: round(v, 2) for k, v in today_pnl.items()},
        "total_pnl": {k: round(v, 2) for k, v in total_pnl.items()},
        "bots": bots, "equity_curve": eq, "history": history, "trade_count": len(rows),
    }


# ── Futures Backtest ─────────────────────────────────────────────────────────

@router.post("/backtest/run")
def run_futures_backtest(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Run a leveraged futures backtest using historical KuCoin candle data."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy
    from backend.models.trade import FuturesBacktest
    from backend.services.futures_backtester import run_futures_backtest as _run

    strategy_id      = req.get("strategy_id")
    pairs            = req.get("pairs", ["BTC/USDT"])
    timeframe        = req.get("timeframe", "15m")
    timerange        = req.get("timerange", "20240101-20240401")
    leverage         = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
    starting_balance = float(req.get("starting_balance", 1000))
    stoploss_pct     = float(req.get("stoploss_pct", 3.0))
    take_profit_pct  = float(req.get("take_profit_pct", 1.5))
    # Position-model cap.
    #   1   → Single position (TradingView default pyramiding=0)
    #   999 → Concurrent / unlimited (take every signal alongside existing)
    # We clamp to [1, 1000] only as a sanity bound to prevent obvious
    # garbage; anything in between is rare since the UI now offers only
    # the two extremes, but we still accept arbitrary integers for
    # power-users hitting the API directly.
    # Default 1 (TV single position). Was 999, which silently turned the
    # default "single" mode into pyramiding for any API caller that omitted
    # this field — a strategy backtests very differently with 999 stacked
    # positions vs 1 (e.g. Bollinger: 484 trades / -8% vs 209 / +9%). The UI
    # always sends 1; this just makes the API safe-by-default and consistent.
    max_concurrent   = max(1, min(1000, int(req.get("max_concurrent_positions", 1))))
    # Position mode: "single" (TV-default stop-and-reverse) | "hedge"
    # (LONG + SHORT can coexist on same pair, no stop-and-reverse) |
    # "concurrent" (legacy pyramiding stack same-direction). Hedge mode
    # is the recommended choice for mean-reversion strategies like
    # Bollinger Bands where stop-and-reverse was killing trades mid-range.
    position_mode    = str(req.get("position_mode", "single")).lower()
    if position_mode not in ("single", "hedge", "concurrent"):
        position_mode = "single"
    # "single" means exactly ONE position per pair (stop-and-reverse on the
    # opposite signal). The backtester only does stop-and-reverse when the
    # concurrent cap is 1, so force it — otherwise a stray max_concurrent
    # would silently degrade single mode into pyramiding.
    if position_mode == "single":
        max_concurrent = 1
    # Margin (risk) per trade as fraction of current balance. Default 5%
    # = $50 margin on $1000 balance. Clamped 1..50% (above 50% is
    # essentially "all-in" and would liquidate the account on the first
    # losing trade at any meaningful leverage).
    risk_pct         = max(1, min(50, float(req.get("risk_per_trade_pct", 5))))
    risk_per_trade   = risk_pct / 100.0
    # SL/TP source: when force_slider_sltp=True, the engine uses the
    # slider's stoploss_pct / take_profit_pct values for every trade,
    # ignoring whatever structural SL/TP the strategy's signal function
    # returns. Default False = strategy's structural values win (faithful
    # to the strategy's design intent — e.g. SMCStrategyTV documents
    # "SL: below/above the structural swing point (dynamic)").
    force_slider    = bool(req.get("force_slider_sltp", False))
    # "Stop beyond structure" buffer (the book's #1 risk rule) — % of entry to
    # push a STRUCTURAL stop further from entry so it sits past the swing level.
    # 0 = off (default; identical results). Clamped to a sane range.
    sl_structure_buffer_pct = max(0.0, min(5.0, float(req.get("sl_structure_buffer_pct", 0.0))))
    # Whether to deduct funding fees + KuCoin taker/maker fees from the
    # simulated balance. Default False = pure strategy P&L (price action
    # × leverage only). When True, the result is what the strategy would
    # actually deliver on KuCoin including all execution costs.
    deduct_costs    = bool(req.get("deduct_real_costs", False))

    # ── Advanced Risk Management (ARM) — partial TP + BE trail + trail-to-TP1 ──
    # All params default to OFF / safe values so existing API callers see
    # no behaviour change unless they explicitly opt in.
    arm_enabled        = bool(req.get("arm_enabled", False))
    arm_tp1_close_pct  = max(1.0, min(99.0, float(req.get("arm_tp1_close_pct", 50.0))))
    arm_be_mode        = str(req.get("arm_be_mode", "leverage"))
    if arm_be_mode not in ("leverage", "manual_pct", "entry"):
        arm_be_mode = "leverage"
    arm_be_buffer_pct  = max(0.0, min(10.0, float(req.get("arm_be_buffer_pct", 1.0))))
    arm_trail_to_tp1   = bool(req.get("arm_trail_to_tp1", True))

    # ── Tick-level SL/TP precision ──
    # When enabled, the engine replaces the legacy "closer to bar open"
    # heuristic for same-bar SL+TP ambiguity with (1) sub-bar replay
    # using 1m candles within each main TF bar (5m+ only — adds ~30s
    # data fetch), and (2) OHLC-path inference fallback for 1m bars or
    # when sub-bar data was ambiguous. Major accuracy gain for 1m scalp
    # backtests where same-bar SL+TP is the dominant exit pattern.
    tick_precision     = bool(req.get("tick_precision", False))

    # ── Fees: VIP tier + maker-only entry mode ──
    # vip_tier 0..12 selects KuCoin's published maker/taker rates per
    # tier. Default 0 (VIP0 = retail default). At VIP12 maker is a
    # rebate (-0.008%).
    # maker_only_entry simulates a post-only limit at the signal price —
    # the entry only "fills" when the next bar's range touches that
    # price; otherwise the signal is dropped (counted in diagnostics).
    vip_tier         = max(0, min(12, int(req.get("vip_tier", 0))))
    maker_only_entry = bool(req.get("maker_only_entry", False))

    # ── Phase 4b: timeframe-aware risk engine ──
    # When True, every signal's SL/TP is routed through risk_engine.compute_tp_sl
    # so backtest behaviour matches the live engine (which uses risk_engine
    # always since Phase 4). Default OFF so existing API callers / auto-tune
    # runs see no change unless they explicitly enable it.
    use_risk_engine  = bool(req.get("use_risk_engine", False))
    # UI flag overrides for the strategy (e.g. {"use_exit_signals": true} to
    # turn StrategyAsh's CHoCH exit on, or {"USE_ATR_STOPS": true} /
    # {"USE_DYNAMIC_EXITS": true} for the LDC). Only booleans/numbers; applied
    # onto the strategy instance in evaluate_strategy.
    _raw_flags       = req.get("strategy_flags")
    strategy_flags   = _raw_flags if isinstance(_raw_flags, dict) and _raw_flags else None

    # Resolve strategy — pull generated_code so the backtester can actually
    # run the user's authored logic instead of pattern-matching the name to
    # one of the hardcoded built-in signal functions.
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    strategy = None
    generated_code: str | None = None
    if strategy_id:
        strategy = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa
            )
        ).scalar_one_or_none()
        if strategy:
            strategy_name  = strategy.name
            generated_code = strategy.generated_code

    result = _run(
        strategy_name    = strategy_name,
        pairs            = pairs,
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = starting_balance,
        stoploss_pct     = stoploss_pct,
        take_profit_pct  = take_profit_pct,
        generated_code   = generated_code,
        max_concurrent_positions = max_concurrent,
        position_mode    = position_mode,
        risk_per_trade   = risk_per_trade,
        force_slider_sltp = force_slider,
        sl_structure_buffer_pct = sl_structure_buffer_pct,
        deduct_real_costs = deduct_costs,
        arm_enabled       = arm_enabled,
        arm_tp1_close_pct = arm_tp1_close_pct,
        arm_be_mode       = arm_be_mode,
        arm_be_buffer_pct = arm_be_buffer_pct,
        arm_trail_to_tp1  = arm_trail_to_tp1,
        tick_precision    = tick_precision,
        vip_tier          = vip_tier,
        maker_only_entry  = maker_only_entry,
        use_risk_engine   = use_risk_engine,
        strategy_flags    = strategy_flags,
        risk_overrides_for_run = (
            (lambda: __import__('backend.services.risk_engine', fromlist=['load_user_risk_overrides']).load_user_risk_overrides(user_id))()
            if use_risk_engine else None
        ),
    )

    if "error" in result:
        return result

    m = result["metrics"]

    # Persist to DB
    bt = FuturesBacktest(
        user_id          = user_id,
        strategy_id      = strategy_id,
        strategy_name    = strategy_name,
        pairs            = ",".join(pairs),
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = starting_balance,
        final_balance    = m["final_balance"],
        total_profit_pct = m["total_profit_pct"],
        total_profit_abs = m["total_profit_abs"],
        win_rate         = m["win_rate"],
        max_drawdown     = m["max_drawdown"],
        total_trades     = m["total_trades"],
        winning_trades   = m["winning_trades"],
        losing_trades    = m["losing_trades"],
        liquidations     = m["liquidations"],
        long_trades      = m["long_trades"],
        short_trades     = m["short_trades"],
        avg_leverage_pnl = m["avg_leverage_pnl"],
        results_json     = select_desc_json(result),
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    return {**result, "id": bt.id}


def select_desc_json(result: dict) -> str:
    """Store only metrics + trade count (not full trade list) to keep DB rows small."""
    import json
    return json.dumps({
        "metrics":      result.get("metrics", {}),
        "trade_count":  len(result.get("trades", [])),
        "equity_curve": result.get("equity_curve", [])[-50:],  # last 50 points
    })


@router.post("/backtest/walk-forward")
def backtest_walk_forward(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Out-of-sample robustness check. Splits the period into N windows, runs
    the strategy on each, and returns per-window results + a verdict
    (robust / mostly_robust / fragile_or_overfit). The anti-overfitting tool:
    a real edge holds up across windows; a curve-fit one only shines in one."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy
    from backend.services.futures_backtester import walk_forward_backtest

    pairs            = req.get("pairs", ["BTC/USDT"])
    timeframe        = req.get("timeframe", "1h")
    timerange        = req.get("timerange", "20240101-20240601")
    n_windows        = max(2, min(12, int(req.get("n_windows", 4))))
    leverage         = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 5)))
    stoploss_pct     = float(req.get("stoploss_pct", 2.0))
    take_profit_pct  = float(req.get("take_profit_pct", 4.0))
    risk_pct         = max(1, min(50, float(req.get("risk_per_trade_pct", 5))))
    deduct_costs     = bool(req.get("deduct_real_costs", False))  # match the backtest UI: OFF unless you tick "Include real trading costs"
    force_slider     = bool(req.get("force_slider_sltp", False))
    maker_only_entry = bool(req.get("maker_only_entry", False))
    vip_tier         = max(0, min(12, int(req.get("vip_tier", 0))))
    arm_enabled      = bool(req.get("arm_enabled", False))

    strategy_name  = req.get("strategy_name", "SimpleTargetStrategy")
    generated_code = None
    sid = req.get("strategy_id")
    if sid:
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == sid,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name  = strat.name
            generated_code = strat.generated_code

    return walk_forward_backtest(
        strategy_name, pairs, timeframe, timerange, n_windows=n_windows,
        leverage=leverage, stoploss_pct=stoploss_pct, take_profit_pct=take_profit_pct,
        risk_per_trade=risk_pct / 100.0, generated_code=generated_code,
        deduct_real_costs=deduct_costs, force_slider_sltp=force_slider,
        maker_only_entry=maker_only_entry, vip_tier=vip_tier, arm_enabled=arm_enabled,
    )


@router.post("/backtest/timeframe-sweep")
def backtest_timeframe_sweep(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Run the SAME strategy + settings across several timeframes and return a
    side-by-side comparison, so you can see which timeframe the strategy is
    actually suited to.

    EXPLORATION TOOL — NOT an optimizer. Do NOT just cherry-pick the single
    most-profitable timeframe (that's curve-fitting and fails live). Look for
    a timeframe that's CONSISTENTLY reasonable and re-test it on a DIFFERENT
    period before trusting it. Each row is a full, independent backtest using
    the exact same logic as /backtest/run; results are NOT persisted.
    """
    from backend.services.futures_backtester import run_futures_backtest as _run
    from backend.models.strategy import Strategy
    from sqlalchemy import or_

    _VALID_TFS = ("1m", "5m", "15m", "30m", "1h", "4h")
    timeframes = req.get("timeframes") or ["5m", "15m", "1h", "4h"]
    # De-dup, keep valid only, cap at 6 so the sweep can't run forever.
    seen: set = set()
    tfs: list[str] = []
    for tf in timeframes:
        if tf in _VALID_TFS and tf not in seen:
            seen.add(tf); tfs.append(tf)
        if len(tfs) >= 6:
            break
    if not tfs:
        return {"error": "no valid timeframes (allowed: 1m, 5m, 15m, 30m, 1h, 4h)"}

    # ── Parse every backtest param exactly like /backtest/run, EXCEPT the
    #    timeframe (which we vary). Shared so each row is apples-to-apples.
    pairs            = req.get("pairs", ["BTC/USDT"])
    timerange        = req.get("timerange", "20240101-20240401")
    leverage         = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
    starting_balance = float(req.get("starting_balance", 1000))
    stoploss_pct     = float(req.get("stoploss_pct", 3.0))
    take_profit_pct  = float(req.get("take_profit_pct", 1.5))
    max_concurrent   = max(1, min(1000, int(req.get("max_concurrent_positions", 999))))
    position_mode    = str(req.get("position_mode", "single")).lower()
    if position_mode not in ("single", "hedge", "concurrent"):
        position_mode = "single"
    risk_pct         = max(1, min(50, float(req.get("risk_per_trade_pct", 5))))
    risk_per_trade   = risk_pct / 100.0
    force_slider     = bool(req.get("force_slider_sltp", False))
    deduct_costs     = bool(req.get("deduct_real_costs", False))
    arm_enabled      = bool(req.get("arm_enabled", False))
    arm_tp1_close_pct = max(1.0, min(99.0, float(req.get("arm_tp1_close_pct", 50.0))))
    arm_be_mode      = str(req.get("arm_be_mode", "leverage"))
    if arm_be_mode not in ("leverage", "manual_pct", "entry"):
        arm_be_mode = "leverage"
    arm_be_buffer_pct = max(0.0, min(10.0, float(req.get("arm_be_buffer_pct", 1.0))))
    arm_trail_to_tp1 = bool(req.get("arm_trail_to_tp1", True))
    tick_precision   = bool(req.get("tick_precision", False))
    vip_tier         = max(0, min(12, int(req.get("vip_tier", 0))))
    maker_only_entry = bool(req.get("maker_only_entry", False))
    use_risk_engine  = bool(req.get("use_risk_engine", False))
    _raw_flags       = req.get("strategy_flags")
    strategy_flags   = _raw_flags if isinstance(_raw_flags, dict) and _raw_flags else None

    # Resolve the strategy code once (same for every timeframe).
    strategy_id   = req.get("strategy_id")
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    generated_code: str | None = None
    if strategy_id:
        strategy = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa
            )
        ).scalar_one_or_none()
        if strategy:
            strategy_name  = strategy.name
            generated_code = strategy.generated_code

    risk_overrides = None
    if use_risk_engine:
        try:
            from backend.services.risk_engine import load_user_risk_overrides
            risk_overrides = load_user_risk_overrides(user_id)
        except Exception:
            risk_overrides = None

    sweep: list[dict] = []
    for tf in tfs:
        try:
            r = _run(
                strategy_name    = strategy_name,
                pairs            = pairs,
                timeframe        = tf,
                timerange        = timerange,
                leverage         = leverage,
                starting_balance = starting_balance,
                stoploss_pct     = stoploss_pct,
                take_profit_pct  = take_profit_pct,
                generated_code   = generated_code,
                max_concurrent_positions = max_concurrent,
                position_mode    = position_mode,
                risk_per_trade   = risk_per_trade,
                force_slider_sltp = force_slider,
                deduct_real_costs = deduct_costs,
                arm_enabled       = arm_enabled,
                arm_tp1_close_pct = arm_tp1_close_pct,
                arm_be_mode       = arm_be_mode,
                arm_be_buffer_pct = arm_be_buffer_pct,
                arm_trail_to_tp1  = arm_trail_to_tp1,
                tick_precision    = tick_precision,
                vip_tier          = vip_tier,
                maker_only_entry  = maker_only_entry,
                use_risk_engine   = use_risk_engine,
                strategy_flags    = strategy_flags,
                risk_overrides_for_run = risk_overrides,
            )
            if isinstance(r, dict) and "error" in r:
                sweep.append({"timeframe": tf, "error": str(r["error"])[:200]})
                continue
            m = r.get("metrics", {})
            sweep.append({
                "timeframe":        tf,
                "total_trades":     m.get("total_trades", 0),
                "win_rate":         m.get("win_rate", 0.0),
                "total_profit_pct": m.get("total_profit_pct", 0.0),
                "total_profit_abs": m.get("total_profit_abs", 0.0),
                "max_drawdown":     m.get("max_drawdown", 0.0),
                "final_balance":    m.get("final_balance", starting_balance),
                "winning_trades":   m.get("winning_trades", 0),
                "losing_trades":    m.get("losing_trades", 0),
                "long_trades":      m.get("long_trades", 0),
                "short_trades":     m.get("short_trades", 0),
            })
        except Exception as exc:
            sweep.append({"timeframe": tf, "error": str(exc)[:200]})

    return {
        "sweep":         sweep,
        "strategy_name": strategy_name,
        "pairs":         pairs,
        "timerange":     timerange,
        "leverage":      leverage,
        # Surfaced to the UI so the user is reminded what this is (and isn't).
        "note": ("Exploration tool — pick a timeframe that is CONSISTENTLY "
                 "reasonable across periods, not the single highest number "
                 "(that is overfitting). Always re-test your pick on a "
                 "different period and paper-trade before going live."),
    }


@router.post("/backtest/auto-tune")
def auto_tune_futures_backtest(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Run the strategy with a small SL/TP grid and return ranked results.

    Used by the 'Auto-tune SL/TP' button on the futures backtest page.
    Each grid cell is a real backtest, so this takes longer than a single
    run (typically 30s-3min for the 4×5 = 20 cell grid). KuCoin data is
    cached across cells so all runs share ONE candle/funding download.

    Returns the best combo + the full grid for the UI to render as a
    heatmap, so the user can see WHICH SL/TP combinations were tested
    and where the cliff is (e.g. 'tight stops always lose, wide TPs
    are never reached').
    """
    from sqlalchemy import or_
    from backend.models.strategy import Strategy
    from backend.services.futures_backtester import auto_tune_sltp

    strategy_id      = req.get("strategy_id")
    pairs            = req.get("pairs", ["BTC/USDT"])
    timeframe        = req.get("timeframe", "15m")
    timerange        = req.get("timerange", "20240101-20240401")
    leverage         = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
    starting_balance = float(req.get("starting_balance", 1000))

    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    generated_code: str | None = None
    if strategy_id:
        strategy = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa
            )
        ).scalar_one_or_none()
        if strategy:
            strategy_name  = strategy.name
            generated_code = strategy.generated_code

    return auto_tune_sltp(
        strategy_name    = strategy_name,
        pairs            = pairs,
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = starting_balance,
        generated_code   = generated_code,
    )


@router.get("/backtest/history")
def futures_backtest_history(
    limit: int = 20,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Return past futures backtest runs for this user."""
    from backend.models.trade import FuturesBacktest
    from sqlalchemy import desc as sql_desc
    rows = db.execute(
        select(FuturesBacktest)
        .where(FuturesBacktest.user_id == user_id)
        .order_by(sql_desc(FuturesBacktest.created_at))
        .limit(limit)
    ).scalars().all()
    return {
        "backtests": [
            {
                "id":               r.id,
                "strategy_name":    r.strategy_name,
                "pairs":            r.pairs,
                "timeframe":        r.timeframe,
                "timerange":        r.timerange,
                "leverage":         r.leverage,
                "starting_balance": r.starting_balance,
                "final_balance":    r.final_balance,
                "total_profit_pct": r.total_profit_pct,
                "win_rate":         r.win_rate,
                "max_drawdown":     r.max_drawdown,
                "total_trades":     r.total_trades,
                "liquidations":     r.liquidations,
                "created_at":       str(r.created_at),
            }
            for r in rows
        ]
    }


# ── One-time cleanup ──────────────────────────────────────────────────────────

@router.delete("/cleanup-test-trades")
def cleanup_test_trades(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Delete all open futures trades for this user (removes stale test records)."""
    from sqlalchemy import delete as sql_delete
    result = db.execute(
        sql_delete(Trade).where(
            Trade.user_id == user_id,
            Trade.market_type == "futures",
            Trade.status == "open",
        )
    )
    db.commit()
    return {"deleted": result.rowcount, "user_id": user_id}


@router.delete("/cleanup-broken-trades")
def cleanup_broken_trades(
    mode: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Delete trades created by the stale _last_prices cache bug (fixed in
    ed1e7c7). Those trades have entry_price == exit_price == cached_value
    and profit_abs == 0 — they're not "real" zero-PNL trades, they're
    artifacts of the engine returning a frozen price from the cache.

    Filtering:
      - User-scoped (only this user's trades)
      - Optional mode filter ('paper' or 'live'); omit to clean both
      - Only deletes CLOSED futures trades
      - Only deletes trades where entry_price == exit_price (the bug
        signature) AND profit_abs == 0

    Safe to call repeatedly. Returns rowcount of deleted records.
    """
    from sqlalchemy import delete as sql_delete, or_
    where_clauses = [
        Trade.user_id == user_id,
        Trade.market_type == "futures",
        Trade.status == "closed",
        Trade.entry_price == Trade.exit_price,
        or_(
            Trade.profit_abs == 0,
            Trade.profit_abs.is_(None),
        ),
    ]
    if mode in ("paper", "live"):
        where_clauses.append(Trade.mode == mode)
    result = db.execute(sql_delete(Trade).where(*where_clauses))
    db.commit()
    return {
        "deleted": result.rowcount,
        "mode_filter": mode or "both",
        "user_id": user_id,
        "explanation": (
            "Removed trades where entry==exit and profit_abs==0 — "
            "artifacts of the stale price-cache bug fixed in ed1e7c7. "
            "Genuine zero-PNL trades (entry != exit, profit_abs == 0) "
            "are NOT deleted by this cleanup."
        ),
    }


# ── Balance ──────────────────────────────────────────────────────────────────

@router.get("/balance")
def futures_balance(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Fetch KuCoin Futures account USDT balance (real money, live accounts only)."""
    from backend.services.native_trading_engine import _kucoin_get_signed
    from backend.utils.encryption import decrypt, DecryptError

    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        return {"error": "Add your KuCoin Futures API key in Setup first.", "balance": None}
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return {"error": "Could not decrypt KuCoin credentials. Re-enter in Setup.", "balance": None}

    # Guard: if keys are blank, no point calling the API
    if not kk or not ks:
        return {"error": "KuCoin Futures API key not configured. Go to Setup → add your Futures key.", "balance": None}

    try:
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        # Correct KuCoin Futures endpoint: /api/v1/account-overview (hyphen, not camelCase)
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"},
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) != "200000":
            msg = data.get("msg", "unknown")
            # KuCoin error codes: 400100 = invalid credentials, 400006 = no permission
            code = str(data.get("code", ""))
            if code in ("400100", "400006", "400200"):
                return {"error": "Invalid API key or missing Futures permissions. Check Setup.", "balance": None}
            return {"error": f"KuCoin Futures API error {code}: {msg}", "balance": None}
        account    = data.get("data", {})
        available  = float(account.get("availableBalance", 0))
        total      = float(account.get("accountEquity", available))
        unrealized = float(account.get("unrealisedPNL", 0))
        return {
            "balance":    round(available, 4),
            "equity":     round(total, 4),
            "unrealized": round(unrealized, 4),
            "currency":   "USDT",
            "type":       "futures",
        }
    except Exception as e:
        err = str(e)
        if "404" in err:
            return {"error": "KuCoin Futures API endpoint not found. Check your Futures API key.", "balance": None}
        if "403" in err or "401" in err:
            return {"error": "KuCoin API permission denied. Ensure your Futures key has 'General' permission.", "balance": None}
        return {"error": f"Could not reach KuCoin Futures: {err}", "balance": None}


# ── Start / Stop ─────────────────────────────────────────────────────────────

@router.post("/start")
def start_futures(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Start a futures trading bot (paper or live)."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy

    strategy_id   = req.get("strategy_id")
    mode          = req.get("mode", "paper")
    pairs         = req.get("pairs", ["BTC/USDT"])
    # PDF §7 hard safety caps (matches POST /bots).
    leverage      = max(1, min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10))))
    timeframe     = req.get("timeframe", "15m")
    stoploss      = max(-0.50, min(-0.001, float(req.get("stoploss", -0.03))))
    wallet        = max(10.0,  min(1_000_000.0, float(req.get("wallet", 1000.0))))
    take_profit   = max(0.1,   min(200.0, float(req.get("take_profit_pct", 1.5))))   # %, not decimal
    max_pos_pct   = max(0.5,   min(25.0,  float(req.get("max_position_pct", 5.0))))

    # Resolve strategy name
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    if strategy_id:
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa: E712
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name = strat.name

    # ── Live Guardrail (PDF §9) — mirrors POST /api/futures/bots ──────
    # Before this commit, /api/futures/start was a back-door that bypassed
    # the confidence/conflict checks the bot panel ran. Closing the hole.
    if mode == "live" and strat:
        try:
            from backend.services.strategy_validator import validate_for_live
            live_ok, template, reason = validate_for_live(
                strategy_name       = strategy_name,
                strategy_id         = strategy_id,
                generated_code      = strat.generated_code or "",
                execution_timeframe = timeframe,
            )
            # Backtest-pass requirement (PDF §9 row 4): live trading is
            # only allowed when a futures backtest has been run for
            # (strategy, this pair, this TF) within the last 30 days.
            from backend.models.trade import FuturesBacktest
            from sqlalchemy import desc as _desc
            primary_pair = (pairs or ["BTC/USDT"])[0]
            recent_bt = db.execute(
                select(FuturesBacktest)
                .where(
                    FuturesBacktest.user_id == user_id,
                    FuturesBacktest.strategy_id == strategy_id,
                    FuturesBacktest.pair == primary_pair,
                    FuturesBacktest.timeframe == timeframe,
                )
                .order_by(_desc(FuturesBacktest.created_at))
                .limit(1)
            ).scalar_one_or_none()
            has_recent_backtest = bool(
                recent_bt and recent_bt.created_at and
                (datetime.utcnow() - recent_bt.created_at).days <= 30
            )

            if not live_ok or not has_recent_backtest:
                bt_msg = (
                    "Run a backtest for this strategy/pair/timeframe in the "
                    "last 30 days before going live."
                ) if not has_recent_backtest else ""
                return {
                    "error": (reason or "Live guardrail blocked") + (" | " + bt_msg if bt_msg else ""),
                    "blocked_reason":    "live_guardrail",
                    "confidence_score":  template.confidence_score,
                    "live_permission":   template.live_permission,
                    "missing_fields":    template.missing_fields,
                    "conflicts":         template.conflicts,
                    "has_recent_backtest": has_recent_backtest,
                    "resolver_notes":    template.resolver_notes,
                }
            # Cache the validation result on the Strategy row.
            try:
                strat.compiled_template = template.to_dict()
                strat.confidence_score  = template.confidence_score
                strat.live_permission   = template.live_permission
                db.commit()
            except Exception:
                pass
        except Exception as guard_exc:
            log.warning("Live guardrail validator raised: %s — allowing live start", guard_exc)

    eng = futures_engine_registry.for_user(user_id)

    kk = ks = kp = ""
    if mode == "live":
        from backend.utils.encryption import decrypt, DecryptError
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if not cfg:
            return {"error": "No config found. Complete Setup first."}
        try:
            kk = decrypt(cfg.kucoin_key_enc or "", user_id)
            ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
            kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
        except Exception:
            return {"error": "Could not decrypt KuCoin credentials. Re-enter in Setup."}

    result = eng.start_futures(
        strategy_name    = strategy_name,
        pairs            = pairs,
        leverage         = leverage,
        mode             = mode,
        timeframe        = timeframe,
        stoploss         = stoploss,
        wallet           = wallet,
        max_position_pct = max_pos_pct,
        take_profit_pct  = take_profit,
        kucoin_key       = kk,
        kucoin_secret    = ks,
        kucoin_passphrase= kp,
        strategy_id      = strategy_id,
    )

    if result.get("started"):
        # Persist futures bot state for auto-resume
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            cfg.bot_running       = True
            cfg.bot_mode          = f"futures-{mode}"
            cfg.bot_strategy_name = strategy_name
            cfg.bot_pairs         = ",".join(pairs)
            cfg.bot_timeframe     = timeframe
            cfg.bot_stoploss      = stoploss
            cfg.bot_wallet        = wallet
            db.commit()

    log_event(db, user_id, "futures.start", request, payload={
        "mode": mode, "leverage": leverage, "pairs": pairs, "strategy": strategy_name
    })
    return result


@router.post("/stop")
def stop_futures(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    eng = futures_engine_registry.for_user(user_id)
    result = eng.stop()
    log_event(db, user_id, "futures.stop", request)
    return result


# ── Status ───────────────────────────────────────────────────────────────────

@router.get("/status")
def futures_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    eng = futures_engine_registry.for_user(user_id)
    return eng.status   # status is a @property, NOT a method — no () needed


# ── Open Positions ────────────────────────────────────────────────────────────

@router.get("/open")
def futures_open_positions(
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import _kucoin_get

    user_eng = futures_engine_registry.for_user(user_id)
    # Collect every futures engine owned by this user — the manual-trading
    # `for_user` engine PLUS one per running bot. Each engine is tagged with
    # its source ('manual' for the user engine, 'bot' + bot_key for bot
    # engines) so the frontend Positions tab can show ONLY manual entries
    # and bot positions live exclusively in the Bots tab. Without this tag,
    # a paper bot trade was leaking into the manual Positions tab.
    bot_engines_kv = futures_engine_registry.user_bot_engines(user_id)
    engines_with_meta = [
        ("manual", None, user_eng),
        *[("bot", bot_key, e) for bot_key, e in bot_engines_kv],
    ]

    # Throttled live-position reconciliation: catches manual closes done
    # via the KuCoin UI, missed liquidations, and post-fill risk-engine
    # rollbacks. Bot engines run this inside _run_loop already; the
    # user-shared manual-trade engine doesn't have a loop, so we trigger
    # it here on every UI poll (capped to once per 30s per engine).
    for _src, _key, _eng in engines_with_meta:
        try:
            # 3s reconcile cadence (was 30s) — picks up manual closes on
            # KuCoin's own UI within 3 seconds. False-removal race during
            # the post-entry window is now defended by the grace period
            # + drift threshold inside _reconcile_live_positions.
            _eng.maybe_reconcile_live_positions(throttle_secs=3)
        except Exception:
            pass

    # Build positions list filtered by mode.
    # Each position may have a _mode tag (manual trades), otherwise use engine mode.
    native_positions = []
    # De-dup by engine trade_key (which is unique per position) plus the
    # (engine_src, bot_key) tuple so that the same trade_key from two
    # different engines doesn't collapse. CRITICAL: was previously
    # de-duping by (pair, direction, mode, source) which collapsed every
    # stacked manual entry on the same pair into ONE visible row even
    # though the engine actually held N of them — and made the Close
    # button effectively a Close-All because pair+direction was the only
    # identifier the UI knew.
    seen_keys: set[tuple[str, str, str]] = set()
    for _src, _bot_key, eng in engines_with_meta:
        with eng._lock:
            for trade_key, p in eng.positions.items():
                pos_mode = getattr(p, "_mode", eng._mode or "paper")
                if mode is not None and pos_mode != mode:
                    continue
                k = (trade_key, _src, _bot_key or "")
                if k in seen_keys:
                    continue
                seen_keys.add(k)
                liq = getattr(p, "liquidation_price", None)
                lev = getattr(p, "leverage", 1)
                # EFFECTIVE size accounts for partial closes. partial_close
                # reduces remaining_pct (e.g. 25% close → remaining_pct=0.75)
                # but leaves pos.size unchanged. Without this multiplication
                # /open reports the ORIGINAL size, the UI snaps back to the
                # full row after a partial — the user clicks 25%, sees the
                # row shrink optimistically, then 8s later it's back to
                # full because /open re-renders pos.size.
                _remaining = float(getattr(p, "remaining_pct", 1.0) or 1.0)
                _effective_size = float(p.size) * max(0.0, min(1.0, _remaining))
                native_positions.append({
                "pair":              p.pair,
                "direction":         p.direction,
                "entry":             round(p.entry, 6),
                "sl":                round(p.effective_sl, 6) if hasattr(p, "effective_sl") else round(p.sl, 6),
                "tp":                round(p.tp, 6),
                "stake":             round(_effective_size, 2),
                "contracts":         int(getattr(p, "contracts", 0) or 0),
                "opened_at":         str(p.opened_at),
                "leverage":          lev,
                "liquidation_price": round(liq, 6) if liq else None,
                # Margin mode for the row badge. Per-symbol setting on the
                # owning engine (falls back to the engine's global default);
                # matches what _place_live_entry sends to KuCoin for live.
                "margin_mode":       eng.get_symbol_margin(
                                         p.pair.replace("/", "").replace("USDT", "USDTM")),
                "_pos_mode":         pos_mode,
                "_source":           _src,
                "bot_key":           _bot_key,
                # Unique per-position handle. Frontend echoes this back
                # on Close / partial-close so the backend can target ONE
                # specific position even when multiple share pair+direction.
                "position_id":       f"eng:{trade_key}",
                "_engine_trade_key": trade_key,
                # db_id of the corresponding Trade row (if any). Used by
                # the merge below to suppress only the SPECIFIC DB row
                # this engine position covers, not every DB row sharing
                # the pair. Without it the engine having 1 position on
                # BTC/USDT was hiding the OTHER 4 DB rows on BTC/USDT
                # whose engine state was lost on backend restart.
                "_db_id":            getattr(p, "db_id", None),
                "exchange_order_id": getattr(p, "exchange_order_id", None),
                # UX#14: Phase-3 ARM state surfaced so the UI shows
                # partial-close history per position.
                "arm_active":        bool(getattr(p, "arm_active", False)),
                "tp1_price":         round(p.tp1_price, 6) if getattr(p, "tp1_price", None) else None,
                "tp2_price":         round(p.tp2_price, 6) if getattr(p, "tp2_price", None) else None,
                "tp1_hit":           bool(getattr(p, "tp1_hit", False)),
                "tp1_close_pct":     round(getattr(p, "tp1_close_pct", 0.0) * 100, 1) if getattr(p, "arm_active", False) else None,
                "remaining_pct":     round(getattr(p, "remaining_pct", 1.0) * 100, 1),
                "partial_pnl_abs":   round(getattr(p, "partial_pnl_abs", 0.0), 4),
                "partial_exits":     list(getattr(p, "partial_exits", []) or []),
                "trailed_to_tp1":    bool(getattr(p, "trailed_to_tp1", False)),
            })

    # Fetch live FUTURES prices (was using spot orderbook — basis bug).
    live_prices: dict[str, float] = {}
    for p in native_positions:
        price = _futures_ticker_price(p["pair"])
        if price is not None:
            live_prices[p["pair"]] = price

    native_trades = []
    for p in native_positions:
        entry     = p.get("entry", 0)
        stake     = p.get("stake", 0)
        leverage  = p.get("leverage", eng._leverage)
        cur       = live_prices.get(p["pair"], entry)
        direction = p.get("direction", "long")
        raw_pnl   = (cur - entry) / entry * stake if (entry and direction == "long") \
                    else (entry - cur) / entry * stake if entry else 0
        lev_pnl   = raw_pnl * leverage
        native_trades.append({
            # Unique per-position id so Close / 25-50-75% target the
            # exact row the user clicked instead of every position
            # matching pair+direction.
            "id":                p.get("position_id") or f"futures-{p['pair']}-{p.get('_pos_mode','paper')}",
            "position_id":       p.get("position_id"),
            # Mirror db_id onto the trade so the merge below can dedup
            # row-by-row instead of by pair.
            "_db_id":            p.get("_db_id"),
            "pair":              p["pair"],
            "side":              direction,
            "entry_price":       entry,
            "current_price":     round(cur, 6),
            "amount":            stake,
            "contracts":         p.get("contracts", 0),
            "leverage":          leverage,
            "liquidation_price": p.get("liquidation_price"),
            "stoploss_price":    p.get("sl"),
            "tp_price":          p.get("tp"),
            "entry_time":        p.get("opened_at"),
            # Use the position's own _mode tag, NOT the engine mode, so a live
            # manual entry placed while the engine is in default-paper still
            # reports mode="live" to the UI.
            "mode":              p.get("_pos_mode") or (eng._mode or "paper"),
            "margin_mode":       p.get("margin_mode"),
            "exchange_order_id": p.get("exchange_order_id"),
            "market_type":       "futures",
            "unrealized_pnl":    round(lev_pnl, 4),
            # Booked profit on partial closes so far. The user clicks
            # 25/50/75% to book a portion; this field surfaces the
            # realized P&L on the closed portions while the remainder
            # keeps running. Shown as "Booked" on each row.
            "partial_pnl_abs":   round(float(p.get("partial_pnl_abs") or 0.0), 4),
            "remaining_pct":     float(p.get("remaining_pct") or 100.0),
            # NEW: 'manual' vs 'bot' tag so the UI can keep the
            # Positions tab clean (manual only) and route bot
            # positions to the Bots tab. Without this, paper bot
            # entries were polluting the manual Positions table.
            "source":            p.get("_source", "manual"),
            "bot_key":           p.get("bot_key"),
        })

    # DB open futures positions
    query = (
        select(Trade)
        .where(Trade.status == "open", Trade.user_id == user_id, Trade.market_type == "futures")
        .order_by(desc(Trade.entry_time))
    )
    if mode:
        query = query.where(Trade.mode == mode)

    db_rows = db.execute(query).scalars().all()

    # Fetch live prices for DB-only pairs not already retrieved above
    pairs_in_native = {p["pair"] for p in native_positions}
    db_only_pairs   = {t.pair for t in db_rows if t.pair not in pairs_in_native}
    for pair_name in db_only_pairs:
        if pair_name not in live_prices:
            price = _futures_ticker_price(pair_name)
            if price is not None:
                live_prices[pair_name] = price

    db_trades = []
    for t in db_rows:
        entry   = t.entry_price or 0
        cur     = live_prices.get(t.pair, entry)
        lev     = t.leverage or 1
        side    = t.side or "long"
        raw_pnl = (cur - entry) / entry if (entry and side == "long") \
                  else (entry - cur) / entry if entry else 0
        unreal  = round(t.amount * raw_pnl * lev, 4) if entry else 0
        db_trades.append({
            "id":                t.id,
            # Unique per-row identifier — string-typed so the frontend
            # can treat engine-backed and DB-fallback rows uniformly.
            # "db:42" cleanly distinguishes from engine keys ("eng:<key>").
            "position_id":       f"db:{t.id}",
            "pair":              t.pair,
            "side":              side,
            "entry_price":       entry,
            "current_price":     round(cur, 6) if cur != entry else 0,
            "amount":            t.amount,
            "leverage":          lev,
            "liquidation_price": t.liquidation_price,
            "stoploss_price":    t.stoploss_price,
            "entry_time":        str(t.entry_time),
            "mode":              t.mode,
            # Trade rows don't persist margin mode; surface the user engine's
            # per-symbol setting (falls back to its global default) so the
            # row badge isn't blank / wrongly "Cross".
            "margin_mode":       user_eng.get_symbol_margin(
                                     t.pair.replace("/", "").replace("USDT", "USDTM")),
            "market_type":       "futures",
            "unrealized_pnl":    unreal,
            # DB-fallback trades have no engine-side bot tag,
            # so they're treated as manual.
            "source":            "manual",
            "bot_key":           None,
        })

    # Merge by db_id, not by pair. Previously any pair that had at least
    # one native engine position caused ALL DB-fallback rows for that pair
    # to be dropped — so when an engine restart left N-1 of N positions
    # without an engine binding, the user saw 1 row in the UI instead of N.
    # Now: a DB row is suppressed only when the engine actually carries it
    # (engine pos.db_id matches Trade.id). Rows the engine doesn't know
    # about pass through and the user can still see and close them.
    native_db_ids: set[int] = {t["_db_id"] for t in native_trades if t.get("_db_id")}
    merged = native_trades + [t for t in db_trades if t["id"] not in native_db_ids]

    # ── Reconcile with KuCoin Lead Trading for live mode ─────────────────
    # Limit orders that fill immediately (e.g. buy-above-market) and any
    # positions opened on KuCoin we don't know about (filled limit orders,
    # external tools, account restored from snapshot) should appear here
    # so the user never has a hidden real-money position.
    if (mode == "live" or mode is None) and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            kc_resp = _kucoin_get_signed(
                "/api/v1/positions",
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(kc_resp.get("code")) == "200000":
                # Map KuCoin futures symbol → app pair: XBTUSDTM → BTC/USDT.
                # Key by (pair, direction) — NOT pair alone. In hedge mode a
                # pair can hold a long AND a short; deduping by pair would let
                # an app-tracked long suppress a real-money KuCoin short on the
                # same pair (a hidden live position). (pair, direction) matches
                # the granularity of kc_open_keys below.
                keys_already_in_merged = {
                    (t["pair"], (t.get("side") or t.get("direction") or "long"))
                    for t in merged
                }
                # Direction of a KuCoin position row, hedge-mode aware.
                # Returns None for a flat (qty==0) row. In hedge mode KuCoin
                # reports currentQty as a positive magnitude and carries the
                # side in positionSide, so a hedge short (qty>0, side=SHORT)
                # would be misclassified as long by sign alone — which would
                # then drop the real short row as "stale/closed" below. Prefer
                # positionSide; one-way mode omits it (or sends "BOTH") so we
                # fall back to the signed-qty rule unchanged.
                def _kc_direction(kp: dict):
                    _q = float(kp.get("currentQty", 0) or 0)
                    if _q == 0:
                        return None
                    _side = str(kp.get("positionSide", "") or "").upper()
                    if _side == "LONG":
                        return "long"
                    if _side == "SHORT":
                        return "short"
                    return "long" if _q > 0 else "short"
                # Authoritative set of (pair, direction) actually OPEN on
                # KuCoin right now. Used both to skip re-adding rows we
                # already show AND to drop stale LIVE rows below (bugs 1/3).
                kc_open_keys: set[tuple[str, str]] = set()
                for kp in (kc_resp.get("data") or []):
                    _dir = _kc_direction(kp)
                    if _dir is None:
                        continue
                    _ksym = kp.get("symbol", "")
                    _kbase = _ksym.replace("USDTM", "").replace("XBT", "BTC")
                    kc_open_keys.add((f"{_kbase}/USDT", _dir))
                for kp in (kc_resp.get("data") or []):
                    direction = _kc_direction(kp)
                    if direction is None:
                        continue   # closed / zero positions
                    kc_sym = kp.get("symbol", "")
                    # Reverse normalize: XBTUSDTM → BTCUSDTM → BTC/USDT
                    base = kc_sym.replace("USDTM", "").replace("XBT", "BTC")
                    pair = f"{base}/USDT"
                    if (pair, direction) in keys_already_in_merged:
                        continue   # already tracking via engine/DB
                    # Skip positions we JUST closed via the app — KuCoin's
                    # /positions is eventually consistent and may still
                    # report them as open for a few seconds. Without
                    # this guard, the user clicks Close, the row vanishes
                    # via optimistic UI, then reappears on the next /open
                    # refresh because KuCoin hadn't propagated yet.
                    if _was_recently_closed(user_id, pair, direction):
                        continue
                    entry     = float(kp.get("avgEntryPrice", 0))
                    cur       = float(kp.get("markPrice", 0)) or entry
                    margin    = float(kp.get("posMargin", 0) or kp.get("maintMargin", 0))
                    lev       = float(kp.get("realLeverage", 0)) or float(kp.get("leverage", 1)) or 1
                    liq       = float(kp.get("liquidationPrice", 0)) or None
                    unreal    = float(kp.get("unrealisedPnl", 0))
                    # Real margin mode from KuCoin (authoritative). Newer API
                    # returns marginMode "ISOLATED"/"CROSS"; older returns a
                    # crossMode boolean. Surfaced so the Positions panel shows
                    # the TRUE mode instead of always "Cross".
                    _kmm = str(kp.get("marginMode", "") or "").upper()
                    if _kmm not in ("ISOLATED", "CROSS"):
                        _kcross = kp.get("crossMode")
                        _kmm = "CROSS" if _kcross is True else "ISOLATED" if _kcross is False else ""
                    merged.append({
                        "id":                f"kucoin-{kc_sym}",
                        "pair":              pair,
                        "side":              direction,
                        "entry_price":       entry,
                        "current_price":     cur,
                        "amount":            round(margin, 4),
                        "leverage":          round(lev, 2),
                        "liquidation_price": liq,
                        "stoploss_price":    None,
                        "tp_price":          None,
                        "entry_time":        kp.get("openingTimestamp") or None,
                        "mode":              "live",
                        "exchange_order_id": None,
                        "market_type":       "futures",
                        "unrealized_pnl":    round(unreal, 4),
                        "margin_mode":       (_kmm.lower() or None),
                        "_source":           "kucoin",
                        # KuCoin-only positions (placed outside the app
                        # or filled limit orders) — surface them as
                        # manual so they still appear in the Positions
                        # tab and never get hidden.
                        "source":            "manual",
                        "bot_key":           None,
                    })

                # ── Drop stale LIVE rows confirmed CLOSED on KuCoin ────────
                # (bugs 1 & 3). KuCoin's LIST is authoritative: any LIVE row
                # whose (pair, direction) is NOT in kc_open_keys is closed on
                # the exchange — whether the user closed it in KuCoin's own UI
                # or its owning engine was lost on a restart (DB row stuck at
                # status="open"). Guards: a position younger than the grace
                # window is never dropped (KuCoin open-propagation lag), and a
                # row must be missing for N consecutive polls (transient
                # currentQty=0 quirk protection). When a row crosses the
                # threshold we drop it from the response, flip its DB row to
                # "closed" so it can't resurrect, and evict it from any engine
                # still holding it.
                import time as _t_recon
                _now_s = _t_recon.time()
                _kept: list[dict] = []
                _stale_db_ids: set[int] = set()
                _stale_eng_keys: set[str] = set()
                # (kc_symbol_upper, close_side) of legs confirmed CLOSED on
                # KuCoin — used to retire their now-dead pending TP/SL stop
                # rows so a later position on the same leg doesn't inherit a
                # stale TP/SL via the overlay. Safe because KuCoin auto-cancels
                # reduceOnly/closeOrder stops when the position closes, so the
                # stop is already gone on the exchange.
                _stale_stop_keys: set[tuple[str, str]] = set()
                for _row in merged:
                    # Only reconcile MANUAL live rows here. Bot live positions
                    # are owned by their bot engine's own reconcile loop (which
                    # books realized P&L on close) — evicting them here would
                    # drop the trade without accounting. Non-live + bot rows
                    # pass straight through untouched.
                    _is_live   = (_row.get("mode") or "").lower() == "live"
                    _is_manual = (_row.get("source") or "manual") == "manual"
                    if not (_is_live and _is_manual):
                        _kept.append(_row)
                        continue
                    _rdir = _row.get("side") or _row.get("direction") or "long"
                    _rkey = (_row["pair"], _rdir)
                    _miss_key = (user_id, _row["pair"], _rdir)
                    if _rkey in kc_open_keys:
                        _live_miss.pop(_miss_key, None)   # confirmed alive
                        _kept.append(_row)
                        continue
                    # Not on KuCoin. Protect freshly-opened positions whose
                    # open KuCoin may not have propagated yet.
                    _age = None
                    try:
                        _et = _row.get("entry_time")
                        if _et and str(_et).strip() and str(_et) != "None":
                            _ds = str(_et).replace("Z", "+00:00")
                            _dt = datetime.fromisoformat(_ds)
                            if _dt.tzinfo is None:
                                from datetime import timezone as _tzu
                                _dt = _dt.replace(tzinfo=_tzu.utc)
                            _age = _now_s - _dt.timestamp()
                    except Exception:
                        _age = None
                    if _age is not None and _age < _LIVE_FRESH_GRACE_SECS:
                        _kept.append(_row)   # too new to judge
                        continue
                    # Recently closed in-app → already handled, drop silently.
                    if _was_recently_closed(user_id, _row["pair"], _rdir):
                        _miss = _LIVE_MISS_DROP_THRESHOLD
                    else:
                        _miss = _live_miss.get(_miss_key, 0) + 1
                        _live_miss[_miss_key] = _miss
                    if _miss >= _LIVE_MISS_DROP_THRESHOLD:
                        _live_miss.pop(_miss_key, None)
                        _did = _row.get("_db_id")
                        if not isinstance(_did, int) and isinstance(_row.get("id"), int):
                            _did = _row["id"]
                        if isinstance(_did, int):
                            _stale_db_ids.add(_did)
                        _pid = _row.get("position_id") or ""
                        if isinstance(_pid, str) and _pid.startswith("eng:"):
                            _stale_eng_keys.add(_pid[4:])
                        try:
                            from backend.services.kucoin_futures_client import normalize_futures_symbol as _nfs_drop
                            _dsym = _nfs_drop(_row["pair"].replace("/", "").replace("USDT", "USDTM")).upper()
                            _dcs  = "sell" if _rdir == "long" else "buy"
                            _stale_stop_keys.add((_dsym, _dcs))
                        except Exception:
                            pass
                        log.info("[%s] /open dropped stale LIVE %s %s — closed on "
                                 "KuCoin (db_id=%s)", user_id, _row["pair"], _rdir, _did)
                        # dropped: do NOT append to _kept
                    else:
                        _kept.append(_row)   # tentative — keep until threshold
                merged = _kept

                # Evict stale positions from any engine still holding them so
                # the next poll doesn't re-surface them from engine state and
                # the manual watchdog stops ticking them.
                if _stale_eng_keys:
                    for _src, _bk, _e in engines_with_meta:
                        with _e._lock:
                            for _k in list(_stale_eng_keys):
                                if _k in _e.positions:
                                    _e.positions.pop(_k, None)

                # Flip stale DB rows to closed so they never resurrect.
                if _stale_db_ids:
                    try:
                        from sqlalchemy import update as _sql_update
                        from datetime import timezone as _tzu2
                        db.execute(
                            _sql_update(Trade)
                            .where(Trade.user_id == user_id, Trade.id.in_(_stale_db_ids),
                                   Trade.status == "open")
                            .values(status="closed",
                                    exit_time=datetime.now(_tzu2.utc),
                                    exit_reason="closed_on_kucoin")
                        )
                        db.commit()
                    except Exception as _close_err:
                        db.rollback()
                        log.warning("[%s] Could not close stale DB rows %s: %s",
                                    user_id, _stale_db_ids, _close_err)

                # Retire the now-dead TP/SL stop rows for legs confirmed
                # closed on KuCoin (KuCoin already cancelled the reduceOnly
                # stops), so the overlay below won't show a stale TP/SL.
                if _stale_stop_keys:
                    try:
                        stale_stop_rows = db.execute(
                            select(FuturesOrder).where(
                                FuturesOrder.user_id == user_id,
                                FuturesOrder.mode == "live",
                                FuturesOrder.order_type.in_(["stop_tp", "stop_sl"]),
                                FuturesOrder.status == "pending",
                            )
                        ).scalars().all()
                        _retired = False
                        for _sr in stale_stop_rows:
                            if ((_sr.symbol or "").upper(), (_sr.side or "").lower()) in _stale_stop_keys:
                                _sr.status = "cancelled"
                                _sr.cancelled_at = datetime.utcnow()
                                _retired = True
                        if _retired:
                            db.commit()
                    except Exception as _sk_err:
                        db.rollback()
                        log.warning("[%s] Could not retire stale live stop rows: %s",
                                    user_id, _sk_err)
        except Exception as e:
            log.warning("[%s] KuCoin position reconcile failed: %s", user_id, e)

    # ── Paper: retire orphaned stop rows whose position is gone ──────────
    # Paper TP/SL is recorded as pending "stop_tp"/"stop_sl" FuturesOrder
    # rows so it shows under Advanced Orders. When the position closes
    # (manual close or an auto TP/SL hit via the watchdog), those rows would
    # linger forever and clutter the tab. Retire any pending paper stop that
    # no longer has a matching OPEN paper position (by symbol + close-side).
    if mode == "paper":
        try:
            from backend.services.kucoin_futures_client import normalize_futures_symbol as _nfs_cl
            open_keys: set[tuple[str, str]] = set()
            for _pp in merged:
                if (_pp.get("mode") or "").lower() != "paper":
                    continue
                try:
                    _s = _nfs_cl(_pp["pair"].replace("/", "").replace("USDT", "USDTM")).upper()
                except Exception:
                    continue
                _cs = "sell" if (_pp.get("side") or _pp.get("direction") or "long") == "long" else "buy"
                open_keys.add((_s, _cs))
            paper_stops = db.execute(
                select(FuturesOrder).where(
                    FuturesOrder.user_id == user_id,
                    FuturesOrder.mode == "paper",
                    FuturesOrder.order_type.in_(["stop_tp", "stop_sl"]),
                    FuturesOrder.status == "pending",
                )
            ).scalars().all()
            _dirty = False
            for _so in paper_stops:
                if ((_so.symbol or "").upper(), (_so.side or "").lower()) not in open_keys:
                    _so.status = "cancelled"
                    _so.cancelled_at = datetime.utcnow()
                    _dirty = True
            if _dirty:
                db.commit()
        except Exception as _cl_err:
            db.rollback()
            log.warning("[%s] paper stop cleanup failed: %s", user_id, _cl_err)

    # ── Overlay TP/SL onto position rows from recorded stop orders ──────
    # A position's TP/SL on KuCoin lives as separate reduceOnly STOP orders
    # ("Advanced Orders"), NOT as a field on the position itself — so a TP/SL
    # the user set showed up only under Advanced Orders, never on the
    # position ROW (the exact bug reported). Here we project the active
    # recorded stops back onto each row by (symbol, close-side) so the
    # Positions tab shows the real TP/SL. Works for live (KuCoin-only rows
    # that previously hard-coded None) AND paper. KuCoin is the ultimate
    # source of truth for live; the frontend additionally projects
    # KuCoin-UI-placed stops it sees in the Open Orders list.
    try:
        from backend.services.kucoin_futures_client import normalize_futures_symbol as _nfs
        stop_rows = db.execute(
            select(FuturesOrder).where(
                FuturesOrder.user_id == user_id,
                FuturesOrder.order_type.in_(["stop_tp", "stop_sl"]),
                FuturesOrder.status == "pending",
            )
        ).scalars().all()
        if stop_rows:
            # Index: (symbol_upper, close_side, "tp"|"sl") -> stop price
            _stop_idx: dict[tuple[str, str, str], float] = {}
            for _s in stop_rows:
                _kind = "tp" if _s.order_type == "stop_tp" else "sl"
                _price = _s.stop_price
                if _price is None:
                    _price = _s.tp_price if _kind == "tp" else _s.sl_price
                if _price is None:
                    continue
                _stop_idx[((_s.symbol or "").upper(), (_s.side or "").lower(), _kind)] = float(_price)
            for _row in merged:
                try:
                    _sym = _nfs(_row["pair"].replace("/", "").replace("USDT", "USDTM")).upper()
                except Exception:
                    continue
                _cside = "sell" if (_row.get("side") or _row.get("direction") or "long") == "long" else "buy"
                _tp = _stop_idx.get((_sym, _cside, "tp"))
                _sl = _stop_idx.get((_sym, _cside, "sl"))
                if _tp is not None:
                    _row["tp_price"] = _tp
                if _sl is not None:
                    _row["stoploss_price"] = _sl
    except Exception as _ov_err:
        log.warning("[%s] TP/SL overlay failed: %s", user_id, _ov_err)

    return {"trades": merged}


# ── Trade History ─────────────────────────────────────────────────────────────

@router.get("/history")
def futures_history(
    mode: str = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    query = (
        select(Trade)
        .where(Trade.status == "closed", Trade.user_id == user_id, Trade.market_type == "futures")
        .order_by(desc(Trade.exit_time))
    )
    if mode:
        query = query.where(Trade.mode == mode)
    query = query.limit(limit).offset(offset)

    trades = [
        {
            "id":           t.id,
            "pair":         t.pair,
            "side":         t.side,
            "entry_price":  t.entry_price,
            "exit_price":   t.exit_price,
            "amount":       t.amount,
            "leverage":     t.leverage,
            "profit_pct":   t.profit_pct,
            "profit_abs":   t.profit_abs,
            "entry_time":   str(t.entry_time),
            "exit_time":    str(t.exit_time),
            "exit_reason":  t.exit_reason,
            "mode":         t.mode,
            "market_type":  "futures",
        }
        for t in db.execute(query).scalars().all()
    ]
    return {"trades": trades}


# ── Manual Entry (futures) ────────────────────────────────────────────────────

@router.post("/manual-entry")
def futures_manual_entry(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Manually open a futures position at the current market price.
    In live mode, places a real order via KuCoin Lead Trading Futures API.
    """
    from backend.services.native_trading_engine import _kucoin_get, _kucoin_post_signed, _persist_open_trade
    from backend.services.futures_engine import FuturesPosition, _calc_liquidation_price, KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    from datetime import datetime, timezone as _tz

    # ── Latency instrumentation ─────────────────────────────────────────
    # Records cumulative ms-since-click at each milestone so Railway logs
    # answer "how many ms when I click Buy/Long". Cheap (perf_counter only).
    # Per-KuCoin-call ms are ALSO logged by _kucoin_post_signed; this adds
    # the TOTAL + the non-KuCoin overhead (price fetch, sizing, DB write).
    _t0 = _time.perf_counter()
    _tmark: dict[str, float] = {}
    def _ck(_name: str) -> None:
        _tmark[_name] = (_time.perf_counter() - _t0) * 1000.0

    pair          = req.get("pair", "BTC/USDT")
    direction     = req.get("direction", "long").lower()
    stake_pct     = float(req.get("stake_pct", 5.0))
    # Frontend can pass cost_usdt directly (preferred for live mode — the
    # stake_pct path multiplies against engine.balance which is the paper
    # wallet default of 1000 USDT, NOT the user's real KuCoin balance, so
    # $5 becomes $48 and KuCoin rejects with "insufficient available margin").
    req_cost_usdt = req.get("cost_usdt")
    req_leverage  = req.get("leverage")
    req_mode      = req.get("mode")  # explicit mode from frontend

    eng = futures_engine_registry.for_user(user_id)

    raw_lev  = int(req_leverage) if req_leverage else (eng._leverage if (eng._leverage and eng._leverage > 1) else 10)
    leverage = min(LEAD_MAX_LEVERAGE, raw_lev)
    # Use explicit mode from request first, then engine mode, then default
    mode     = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")
    balance  = eng.balance   if eng.balance   else 1000.0

    # ── SL/TP are now OPTIONAL and USER-DRIVEN ────────────────────────────
    # Previously this function ALWAYS computed an SL/TP from the engine's
    # default stoploss/take-profit %, so every manual trade silently got
    # stops the user never asked for (bug 4). Now we ONLY apply stops the
    # user explicitly sent. The frontend passes either:
    #   • sl_price / tp_price — an absolute price (preferred), or
    #   • sl_pct  / tp_pct    — a price-move % off entry (convenience).
    # Anything absent/zero means "no stop" → stored as 0 → never fires
    # (Position.check_exit and _push_live_tp_sl both skip falsy legs).
    def _opt_float(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    req_sl_price = _opt_float(req.get("sl_price"))
    req_tp_price = _opt_float(req.get("tp_price"))
    req_sl_pct   = _opt_float(req.get("sl_pct"))
    req_tp_pct   = _opt_float(req.get("tp_pct"))

    # Fetch current FUTURES price (was hitting spot — basis bug).
    entry_price = _futures_ticker_price(pair)
    _ck("price")
    if entry_price is None:
        return {"error": f"Could not fetch futures price for {pair}"}

    # User's *intended* margin in USDT.
    # Priority 1: explicit cost_usdt from the frontend (live market orders).
    # The stake_pct path is broken for live: it multiplies against
    # engine.balance, which is the paper-wallet default of 1000 USDT when
    # no live bot is running, NOT the user's real KuCoin balance. So $5
    # typed becomes $48 sent, KuCoin rejects "insufficient available margin".
    # Priority 2: stake_pct × engine.balance (paper mode + legacy callers).
    if req_cost_usdt is not None:
        try:
            user_cost = float(req_cost_usdt)
        except (TypeError, ValueError):
            user_cost = balance * (stake_pct / 100)
    else:
        user_cost = balance * (stake_pct / 100)
    # `stake` is what we eventually store on the position. For live mode it
    # gets overwritten with the REAL margin KuCoin will lock (after lot-size
    # rounding); for paper mode it stays equal to user_cost.
    stake = user_cost

    # Resolve final SL/TP prices from the user's request ONLY. Absolute
    # price wins; otherwise derive from a price-move %. 0 = not set.
    if req_sl_price is not None:
        sl_price = round(req_sl_price, 6)
    elif req_sl_pct is not None:
        sl_price = round(entry_price * (1 - req_sl_pct / 100), 6) if direction == "long" \
                   else round(entry_price * (1 + req_sl_pct / 100), 6)
    else:
        sl_price = 0.0   # no stop-loss requested

    if req_tp_price is not None:
        tp_price = round(req_tp_price, 6)
    elif req_tp_pct is not None:
        tp_price = round(entry_price * (1 + req_tp_pct / 100), 6) if direction == "long" \
                   else round(entry_price * (1 - req_tp_pct / 100), 6)
    else:
        tp_price = 0.0   # no take-profit requested

    # Hedge mode: when allow_hedge=true we permit opposite directions
    # on the same pair (long + short coexist for hedging). Default is
    # false so a careless click can't accidentally stack two longs on
    # one pair. Matches KuCoin's hedge-mode position-side handling
    # since order body already carries the correct positionSide.
    allow_hedge = bool(req.get("allow_hedge", False))
    with eng._lock:
        if allow_hedge:
            # Block only SAME direction. Opposite directions ok.
            existing_same_dir = [
                p.pair for p in eng.positions.values()
                if getattr(p, "_mode", eng._mode) == mode
                and p.pair == pair
                and p.direction == direction
            ]
            blocked = bool(existing_same_dir)
        else:
            existing_pairs = [p.pair for p in eng.positions.values()
                              if getattr(p, "_mode", eng._mode) == mode]
            blocked = pair in existing_pairs
    if blocked:
        return {"error": (
            f"Already have an open {direction.upper() if allow_hedge else ''} position for {pair}. "
            f"Close it first" + (" or use a different direction." if allow_hedge else ".")
        )}

    # ── Live mode: place real order via Lead Trading API ──────────────
    # CRITICAL: must talk to KuCoin BEFORE we mutate engine state. If the API
    # rejects (no creds, balance too low, bad symbol, etc.) we return early
    # without leaving a phantom position in the engine.
    exchange_order_id = None
    real_notional = real_margin = None
    contracts = None   # KuCoin contract count — set by live sizing OR paper parity below
    if mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": err}
        kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

        # Lot-size-aware sizing. Bails out with a user-facing error BEFORE we
        # touch the exchange when the requested cost is below the symbol's
        # minimum order at this leverage.
        contracts, real_margin, real_notional, sz_err = _compute_live_sizing(
            cost_usdt=user_cost, leverage=leverage,
            price=entry_price, kc_symbol=kc_symbol,
        )
        if sz_err:
            return {"error": sz_err}
        _ck("sizing")

        try:
            side          = "buy" if direction == "long" else "sell"
            position_side = "LONG" if direction == "long" else "SHORT"
            client_oid    = f"atf-manual-{int(_time.time()*1000)}"
            margin_mode   = eng.get_symbol_margin(kc_symbol).upper() or "ISOLATED"

            # Sync the user's chosen margin mode + leverage to KuCoin before
            # placing the order. Without this, KuCoin remembers whatever mode
            # the symbol was last in and rejects the order with
            # "The order's margin mode does not match the selected one".
            # Both syncs are cached per (user, symbol) — back-to-back trades
            # at the same leverage/margin skip these calls entirely.
            sync_ok, sync_err = _sync_margin_mode_to_kucoin(
                eng, kc_symbol, margin_mode, user_id
            )
            if not sync_ok:
                return {"error": sync_err}
            # NOTE: leverage sync moved OFF the critical path — it now runs in
            # the background thread below (with the TP/SL push). The order body
            # carries `leverage` and KuCoin opens the position at exactly that
            # leverage (the bot engine's _place_live_entry proves this: it never
            # calls changeLeverage and opens at the right leverage via the body
            # alone). The changeLeverage call only keeps the per-symbol default
            # in sync for other tooling, so blocking the click on it just burned
            # ~100-300ms on the first trade per symbol for no execution benefit.

            # NOTE: previously this called _fetch_kucoin_symbol_settings to
            # READ BACK KuCoin's effective leverage/margin_mode (Cross often
            # carries a per-symbol override). That added ~300ms per trade.
            # Removed for speed — the sync we just did already enforced our
            # values, and any KuCoin override will be picked up by the
            # reconcile that runs every 30s on /open. The order body still
            # carries our requested values, which is the authoritative path.

            body = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       kc_symbol,
                "type":         "market",
                "size":         contracts,
                "leverage":     leverage,
                "marginMode":   margin_mode,
                "positionSide": position_side,
            }
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(resp.get("code")) != "200000":
                msg = resp.get("msg") or resp
                log.warning("[%s] Lead Trading manual entry rejected: %s", user_id, resp)
                return {"error": f"KuCoin Lead Trading rejected the order: {msg}"}
            exchange_order_id = resp.get("data", {}).get("orderId")
            _ck("order")
            log.info("[%s] Lead Trading manual ENTRY ok: order_id=%s cost=%.2f "
                     "real_margin=%.2f notional=%.2f body=%s",
                     user_id, exchange_order_id, user_cost, real_margin,
                     real_notional, body)
        except Exception as e:
            log.exception("[%s] Lead Trading manual entry failed", user_id)
            return {"error": f"Lead Trading order failed: {e}"}
        # Use the REAL margin (what KuCoin actually locked) as the position
        # size — this is what shows in Positions table, History P&L, etc.
        # If we kept user_cost the app would show $1 while KuCoin shows $79.
        stake = real_margin

    # ── Paper mode: compute the SAME KuCoin contract count for parity ──
    # Display-only — paper P&L stays price-ratio based on `stake`. This makes
    # paper a true dry-run: the user sees exactly how many contracts a LIVE
    # order of this size would send for THIS coin (BTC or non-BTC) via the
    # identical _compute_live_sizing path, so they can validate sizing
    # risk-free before flipping to live. We DON'T block paper on a sub-minimum
    # cost (paper is for experimentation) — we surface a note instead.
    paper_sizing_note = None
    if mode != "live":
        kc_symbol_paper = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        p_contracts, _p_margin, p_notional, p_err = _compute_live_sizing(
            cost_usdt=user_cost, leverage=leverage,
            price=entry_price, kc_symbol=kc_symbol_paper,
        )
        if p_err:
            # Below the symbol's per-contract minimum at this leverage — a live
            # order would be REJECTED. Surface the note (don't block paper).
            contracts = 0
            paper_sizing_note = p_err
        else:
            contracts = p_contracts
            real_notional = p_notional   # show notional in the paper response too

    now = datetime.now(_tz.utc)
    pos = FuturesPosition(
        pair=pair, direction=direction,
        entry=entry_price, sl=sl_price, tp=tp_price,
        size=stake, leverage=leverage, opened_at=now,
    )
    pos._mode = mode  # tag position with its mode for filtering
    pos.contracts = int(contracts or 0)  # KuCoin contract count (parity)
    if exchange_order_id:
        # Stash the exchange order id on the position so /force-close can
        # reconcile with KuCoin even if the engine restarts.
        pos.exchange_order_id = exchange_order_id

    pos_key = f"{pair}-{direction}-manual-{int(now.timestamp())}"
    with eng._lock:
        eng.positions[pos_key] = pos
    # Invalidate the recently-closed guard for THIS (pair, direction) —
    # the user is explicitly re-opening, so any stale KuCoin-side echo
    # from the previous close is now superseded by a real new position.
    # Without this, a quick close → re-open cycle (especially across an
    # engine restart) could leave the new position invisible because
    # /open's reconcile skipped KuCoin's report of it.
    _recently_closed.pop((user_id, pair, direction), None)

    db_id = _persist_open_trade(
        user_id, pos, mode,
        strategy_id  = eng._strategy_id,
        leverage     = leverage,
        market_type  = "futures",
    )
    pos.db_id = db_id
    _ck("persist")

    # ── Live mode: push TP/SL to KuCoin so the stops actually fire ───
    # Fire-and-forget on a background thread so the HTTP response
    # returns the moment the entry order succeeded (~300ms instead of
    # ~900ms with TP/SL inline). The push places up to two stop orders
    # (TP + SL) on the Lead Trading orders endpoint; blocking the
    # response on that was the biggest single source of user-perceived
    # latency on live manual buys.
    #
    # Without the stops, the local sl/tp values are decoration — the
    # exchange's liquidation level is the only real protection. The
    # background push lands within ~1s of the response.
    if mode == "live":
        import threading as _t
        def _bg_push_tp_sl():
            kc_sym_bg = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
            # Best-effort: keep KuCoin's per-symbol leverage default in sync.
            # Off the critical path — the order already opened at the right
            # leverage via the order body. Cached per (user, symbol) so this
            # is a no-op on back-to-back same-leverage trades.
            try:
                _sync_leverage_to_kucoin(eng, kc_sym_bg, leverage, user_id)
            except Exception as lev_exc:
                log.warning("[%s] background leverage sync failed for %s: %s",
                            user_id, kc_sym_bg, lev_exc)
            try:
                eng._push_live_tp_sl(
                    kc_sym_bg, sl_price, tp_price,
                    label="manual_entry", pos=pos,
                )
            except Exception as tpsl_exc:
                log.warning("[%s] Manual entry TP/SL push (background) failed: %s "
                            "(position is open, stops are local-only — user can "
                            "re-push via the TP/SL editor on the Positions tab)",
                            user_id, tpsl_exc)
        _t.Thread(target=_bg_push_tp_sl, daemon=True,
                  name=f"tp-sl-push-{user_id}-{pair}").start()

    log_event(db, user_id, "futures.manual_entry", request, payload={
        "pair": pair, "direction": direction, "entry": entry_price,
        "leverage": leverage, "mode": mode, "exchange_order_id": exchange_order_id,
        "margin": stake, "notional": real_notional,
    })
    _ck("done")
    # Cumulative ms-since-click at each milestone. price=price ready,
    # sizing=lot-size math done, order=KuCoin order confirmed, persist=DB
    # row written, done=response built (log_event commit included). The
    # gap order→persist→done is local/DB work; price→order is the KuCoin
    # round-trip(s). TP/SL push + leverage sync are off-thread (not counted).
    log.info("[%s] TIMING manual-entry %s %s %s  total=%.0fms  "
             "[price@%.0f sizing@%.0f order@%.0f persist@%.0f done@%.0f]",
             user_id, mode, pair, direction, _tmark.get("done", 0.0),
             _tmark.get("price", 0.0), _tmark.get("sizing", 0.0),
             _tmark.get("order", 0.0), _tmark.get("persist", 0.0),
             _tmark.get("done", 0.0))
    return {
        "entered": True,
        "pair": pair,
        "direction": direction,
        "entry": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "liq": pos.liquidation_price,
        "leverage": leverage,
        "mode": mode,
        "exchange_order_id": exchange_order_id,
        "margin": round(stake, 4),                   # what KuCoin actually locked
        "notional": real_notional,                    # position value at entry
        "contracts": int(contracts or 0),             # KuCoin contract count (live + paper parity)
        "sizing_note": paper_sizing_note,             # set only when paper cost < per-contract min
        # Position payload in the SAME shape /api/futures/open emits so
        # the frontend can optimistically prepend the new row instead of
        # waiting on a full panel refresh (~1-2 second perceived latency
        # for the "click Buy → row appears" round-trip).
        "position": {
            "id":                f"eng:{pos_key}",
            "position_id":       f"eng:{pos_key}",
            "_db_id":            db_id,
            "pair":              pair,
            "side":              direction,
            "entry_price":       entry_price,
            "current_price":     entry_price,
            "amount":            round(stake, 4),
            "contracts":         int(contracts or 0),
            "leverage":          leverage,
            "liquidation_price": pos.liquidation_price,
            "stoploss_price":    sl_price,
            "tp_price":          tp_price,
            "entry_time":        str(now),
            "mode":              mode,
            "exchange_order_id": exchange_order_id,
            "market_type":       "futures",
            "unrealized_pnl":    0.0,
            "source":            "manual",
            "bot_key":           None,
        },
    }


# ── Force Close ───────────────────────────────────────────────────────────────

@router.post("/force-close/{pair:path}")
def futures_force_close(
    pair: str,
    request: Request,
    body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Close ALL open futures positions for the given pair.
    In live mode, places close orders via KuCoin Lead Trading API.

    SYNC endpoint (not async): the KuCoin close goes through blocking
    urllib (_kucoin_post_signed). A `async def` here would run the
    blocking call ON the event loop and freeze EVERY other request
    (the /open poll, the price ticker, a second trade) until KuCoin
    replied — up to 8s on a stuck proxy. As a plain `def`, FastAPI
    runs it in its worker threadpool, so a close in flight never
    stalls the rest of the app. Body is read via the `body` param
    instead of `await request.json()` so no await is needed."""
    from backend.services.native_trading_engine import _kucoin_get, _kucoin_post_signed, _persist_closed_trade
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    from sqlalchemy import update as sql_update
    from datetime import timezone as _tz

    # ── Latency instrumentation (see /manual-entry for rationale) ───────
    _t0 = _time.perf_counter()
    _tmark: dict[str, float] = {}
    def _ck(_name: str) -> None:
        _tmark[_name] = (_time.perf_counter() - _t0) * 1000.0

    # Parse optional JSON body for mode + direction + position_id
    req_mode = None
    req_direction = None
    req_position_id: str | None = None
    try:
        if isinstance(body, dict):
            req_mode = body.get("mode")
            # Optional — when present, close only the matching long OR short.
            # Without it the legacy behaviour (close every position on the
            # pair) is kept for backwards compat with old callers.
            d = body.get("direction")
            if d in ("long", "short"):
                req_direction = d
            # Optional — targets ONE specific row by its engine trade_key
            # ("eng:<key>") or DB Trade id ("db:<id>"). When present, all
            # other filters are subordinate to this id so Close on row N
            # affects ONLY row N even when row M shares pair+direction.
            pid = body.get("position_id")
            if isinstance(pid, str) and pid:
                req_position_id = pid
    except Exception:
        pass

    eng = futures_engine_registry.for_user(user_id)

    # Fetch live exit price
    # Fetch FUTURES exit price (was hitting spot — basis bug).
    exit_price = _futures_ticker_price(pair)
    _ck("price")

    now = datetime.now(_tz.utc)
    closed_positions = []
    # Use explicit mode from request first, then engine mode, then default
    mode = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")

    # ── Step 1: identify in-memory positions WITHOUT popping yet ────────
    # Close is transactional for live mode: we only mutate engine/DB state
    # AFTER KuCoin confirms. If KuCoin rejects (mode mismatch, dead lot
    # size, etc.) the in-memory position stays and the user gets a real
    # error to retry with, instead of "everything looks gone here but
    # KuCoin still has it" — which was the previous bug.
    # Decode position_id ("eng:<trade_key>" or "db:<id>") into separate
    # targets. When position_id points to a DB row, the engine match
    # below should match nothing — and the DB orphan path further down
    # picks up the single targeted row.
    target_eng_key: str | None = None
    target_db_id: int | None = None
    if req_position_id:
        if req_position_id.startswith("eng:"):
            target_eng_key = req_position_id[4:]
        elif req_position_id.startswith("db:"):
            try:
                target_db_id = int(req_position_id[3:])
            except ValueError:
                target_db_id = None

    with eng._lock:
        matching = [
            (k, p) for k, p in eng.positions.items()
            if p.pair == pair
            and getattr(p, "_mode", eng._mode or "paper") == mode
            and (req_direction is None or p.direction == req_direction)
            # When the caller targets a specific row, every other engine
            # position is rejected — even when pair + direction + mode
            # would otherwise match.
            and (target_eng_key is None or k == target_eng_key)
            and (target_db_id is None)
        ]

    # ── Step 2: place close orders on KuCoin Lead Trading ────────────────
    kucoin_errors: list[str] = []
    confirmed_keys: list[str] = []   # keys whose KuCoin close confirmed → safe to pop

    if mode == "live" and matching:
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {
                "error": f"Could not load KuCoin credentials: {err}. "
                         "Position is still open — re-enter your API key in Setup and retry.",
                "kucoin_cancelled": False,
            }

        kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

        # Skip the pre-fetch — use the position's own leverage + engine's
        # cached margin mode. The pre-fetch was adding ~300ms to every
        # close. It was defensive against the case where the user's
        # margin mode changed between open and close, but with the
        # per-symbol settings cache (commit 94d945b) the engine value
        # is reliable. On the rare KuCoin "margin mode mismatch"
        # rejection the user just retries — same cost they pay anyway
        # on any rejection. Net: close button feels ~300ms snappier.
        kc_margin_mode = (eng.get_symbol_margin(kc_symbol).upper() or "ISOLATED")
        kc_leverage_real: int | None = None

        for trade_key, pos in matching:
            try:
                side          = "sell" if pos.direction == "long" else "buy"
                position_side = "LONG" if pos.direction == "long" else "SHORT"
                pos_lev       = kc_leverage_real or int(getattr(pos, "leverage", eng._leverage or 10))
                # Bug-fix: per-symbol contract sizing. The old formula
                #   contracts = pos.size * lev / entry * 1000
                # bakes in BTC's 0.001 multiplier (×1000 == ÷0.001), so for
                # every non-BTC symbol it over-sent contracts by 10×–1,000,000×
                # (ETH 0.01, XRP 10, DOGE 1000 …). It only "worked" because
                # KuCoin silently clamps an oversized reduceOnly order down to
                # the open size — if the exchange ever rejected instead, the
                # close would fail. Now match the entry path's _futures_lot_size
                # multiplier and the position's remaining fraction so the size
                # is exact and consistent with how the position was opened.
                _lot          = _futures_lot_size(kc_symbol)
                _remaining    = float(getattr(pos, "remaining_pct", 1.0) or 1.0)
                _base_qty     = (pos.size * pos_lev) / pos.entry
                contracts     = max(1, int(_base_qty / max(_lot, 1e-9) * _remaining))
                body = {
                    "clientOid":   f"atf-close-{int(_time.time()*1000)}",
                    "side":         side,
                    "symbol":       kc_symbol,
                    "type":         "market",
                    "size":         contracts,
                    "leverage":     min(LEAD_MAX_LEVERAGE, pos_lev),
                    "marginMode":   kc_margin_mode,
                    "positionSide": position_side,
                    "reduceOnly":   True,
                }
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                code = str(resp.get("code", ""))
                if code == "200000":
                    confirmed_keys.append(trade_key)
                    log.info("[%s] Lead Trading CLOSE ok for %s: %s",
                             user_id, pair, resp.get("data"))
                    # Mark so the next /open reconcile skips the stale
                    # KuCoin "still open" echo for ~30s. Without this,
                    # the row vanishes via optimistic UI then re-appears
                    # because KuCoin's /positions endpoint hadn't
                    # propagated the close yet.
                    _mark_recently_closed(user_id, pair, pos.direction)
                else:
                    msg = resp.get("msg") or f"KuCoin code {code}"
                    kucoin_errors.append(msg)
                    log.warning("[%s] Lead Trading CLOSE rejected for %s: %s",
                                user_id, pair, resp)
            except Exception as e:
                kucoin_errors.append(str(e))
                log.error("[%s] Lead Trading close failed for %s: %s", user_id, pair, e)

        _ck("kucoin_close")
        # If ANY close was rejected, surface the error and don't touch
        # local state — the user retries with the real reason in hand.
        if kucoin_errors:
            return {
                "error": (
                    "KuCoin Lead Trading rejected the close: "
                    + "; ".join(kucoin_errors)
                    + ". Position still open on KuCoin — please retry, or "
                      "close it directly on the KuCoin tab."
                ),
                "kucoin_cancelled": False,
            }

    # ── Step 3: paper mode OR live confirmed → pop + persist ────────────
    keys_to_finalize = (
        [k for k, _ in matching] if mode == "paper"
        else confirmed_keys
    )
    with eng._lock:
        for trade_key in keys_to_finalize:
            pos = eng.positions.pop(trade_key, None)
            if pos is None:
                continue
            ep = exit_price or pos.entry
            pos.close(ep, "force_closed", now)
            # Credit the wallet only for paper positions on the paper engine.
            # The shared manual engine is paper-mode; a LIVE force-close must
            # not pollute the paper wallet (KuCoin owns live equity). Final leg
            # only — any prior partial already credited balance and is included
            # in pos.pnl_abs, so adding it whole would double-count.
            pos_mode = getattr(pos, "_mode", None) or eng._mode
            if pos_mode == eng._mode:
                eng.balance += pos.pnl_abs - getattr(pos, "partial_pnl_abs", 0.0)
            eng.closed_trades.append(pos)
            closed_positions.append(pos)

    # Persist each closed in-memory position
    total_pnl = 0.0
    for pos in closed_positions:
        # Anti-double-count when force-close completes a position that
        # had prior partial closes booked as separate Trade rows.
        # pos.pnl_abs after pos.close() = final_leg_pnl + partial_pnl_abs.
        # The partial rows already recorded their leg P&L, so the final
        # row should carry only the FINAL leg's P&L to keep totals
        # additive. Same anti-double-count pattern as the partial-close
        # path's full-close branch.
        _saved_pnl = pos.pnl_abs
        try:
            partial_acc = float(getattr(pos, "partial_pnl_abs", 0.0) or 0.0)
            if abs(partial_acc) > 0.0001:
                pos.pnl_abs = pos.pnl_abs - partial_acc
            _persist_closed_trade(user_id, pos, mode, eng._strategy_id, pos.db_id)
        finally:
            pos.pnl_abs = _saved_pnl
        total_pnl += pos.pnl_abs

    # ── Also close any orphaned open DB positions for this pair + mode ──
    orphan_query = select(Trade).where(
        Trade.user_id    == user_id,
        Trade.pair       == pair,
        Trade.market_type == "futures",
        Trade.status     == "open",
    )
    if mode:
        orphan_query = orphan_query.where(Trade.mode == mode)
    # Same direction filter as engine matching — without it a user closing a
    # LONG would also wipe their orphaned SHORT row from the DB.
    if req_direction:
        orphan_query = orphan_query.where(Trade.side == req_direction)
    # When the caller targets a specific row, narrow the orphan close to
    # just that DB id. This is what makes "Close on row N" actually
    # close only row N when N rows share pair+direction+mode.
    if target_db_id is not None:
        orphan_query = orphan_query.where(Trade.id == target_db_id)
    elif target_eng_key is not None:
        # Engine target → don't touch the DB orphans (they're a different
        # set of rows). Otherwise close-on-engine-row would also wipe
        # DB-only rows on the same pair.
        orphan_query = orphan_query.where(Trade.id == -1)  # match nothing
    orphan_trades = db.execute(orphan_query).scalars().all()

    for t in orphan_trades:
        ep = exit_price or t.entry_price
        t.exit_price  = ep
        t.exit_time   = now
        t.exit_reason = "force_closed"
        t.status      = "closed"
        side = getattr(t, "side", "long") or "long"
        if side == "short":
            t.profit_pct = round((t.entry_price - ep) / t.entry_price * 100 * (t.leverage or 1), 4)
        else:
            t.profit_pct = round((ep - t.entry_price) / t.entry_price * 100 * (t.leverage or 1), 4)
        t.profit_abs  = round(t.amount * t.profit_pct / 100, 4)
        total_pnl    += t.profit_abs

    if orphan_trades:
        db.commit()

    # ── Live mode: also close any KuCoin-only positions for this pair ───
    # These appear when a limit order filled immediately on KuCoin and we
    # never created an engine/DB Position to track it. Without this step
    # /force-close would say "No open position" even though one exists on
    # KuCoin holding real margin.
    kucoin_only_closed = 0
    if mode == "live" and not closed_positions and not orphan_trades:
        ok, _ = _ensure_live_credentials(eng, user_id, db)
        if ok:
            try:
                kc_symbol = normalize_futures_symbol(
                    pair.replace("/", "").replace("USDT", "USDTM")
                )
                # Get the live position for this symbol.
                # NOTE: We use /api/v1/positions (LIST) and filter, not
                # /api/v1/position?symbol=X (SINGLE). The single-position
                # endpoint returns qty=0 for Lead Trading positions even
                # when they're real and visible in the LIST endpoint
                # (which is what powers the reconcile that surfaces them
                # in the UI). Using the LIST guarantees we see the same
                # positions the user sees.
                from backend.services.native_trading_engine import _kucoin_get_signed
                pos_resp = _kucoin_get_signed(
                    "/api/v1/positions",
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                pdata: dict = {}
                qty = 0
                row_dir = "long"
                if str(pos_resp.get("code")) == "200000":
                    for _p in (pos_resp.get("data") or []):
                        if (_p.get("symbol") or "").upper() != kc_symbol.upper():
                            continue
                        _q = int(_p.get("currentQty", 0) or 0)
                        if _q == 0:
                            continue
                        # Hedge-mode aware direction: prefer positionSide
                        # (hedge reports qty as a positive magnitude, side in
                        # positionSide); fall back to qty sign for one-way mode.
                        # Without this, a hedge short (qty>0) would be filtered
                        # as long and could be closed with the wrong side.
                        _pside = str(_p.get("positionSide", "") or "").upper()
                        if _pside == "LONG":
                            _row_dir = "long"
                        elif _pside == "SHORT":
                            _row_dir = "short"
                        else:
                            _row_dir = "long" if _q > 0 else "short"
                        # Respect direction filter.
                        if req_direction in ("long", "short") and _row_dir != req_direction:
                            continue
                        pdata = _p
                        qty = _q
                        row_dir = _row_dir
                        break
                if qty != 0:
                    direction = row_dir
                    side          = "sell" if direction == "long" else "buy"
                    position_side = "LONG" if direction == "long" else "SHORT"
                    contracts     = abs(qty)
                    lev_use       = int(pdata.get("realLeverage") or pdata.get("leverage") or 1)
                    body = {
                        "clientOid":   f"atf-kucoin-close-{int(_time.time()*1000)}",
                        "side":         side,
                        "symbol":       kc_symbol,
                        "type":         "market",
                        "size":         contracts,
                        "leverage":     min(LEAD_MAX_LEVERAGE, lev_use),
                        "marginMode":   (pdata.get("marginMode") or "ISOLATED").upper(),
                        "positionSide": position_side,
                        "reduceOnly":   True,
                    }
                    resp = _kucoin_post_signed(
                        "/api/v1/copy-trade/futures/orders", body,
                        eng._api_key, eng._api_sec, eng._api_pass,
                        base_url=KUCOIN_FUTURES_BASE,
                    )
                    if str(resp.get("code")) == "200000":
                        kucoin_only_closed = 1
                        log.info("[%s] Closed KuCoin-only position for %s qty=%s",
                                 user_id, pair, qty)
                        # Same anti-reappear guard as the engine close
                        # path — KuCoin /positions may still report this
                        # open for ~30s; skip in reconcile until then.
                        _mark_recently_closed(user_id, pair, direction)
                    else:
                        log.warning("[%s] Failed to close KuCoin-only position for %s: %s",
                                    user_id, pair, resp)
                        return {"error": f"KuCoin rejected close: {resp.get('msg') or resp}"}
                else:
                    log.info("[%s] /api/v1/positions returned no open qty for %s (code=%s)",
                             user_id, kc_symbol, pos_resp.get("code"))
            except Exception as e:
                log.error("[%s] KuCoin-only close attempt failed: %s", user_id, e)

    total_closed = len(closed_positions) + len(orphan_trades) + kucoin_only_closed
    if total_closed == 0:
        return {"error": f"No open futures position for {pair}"}

    log_event(db, user_id, "futures.force_close", request,
              payload={"pair": pair, "exit_price": exit_price, "count": total_closed})
    _ck("done")
    # Cumulative ms-since-click: price=exit price ready, kucoin_close=KuCoin
    # reduce-only close confirmed (live only), done=response built (DB
    # persist + log_event commit included). price→kucoin_close is the
    # KuCoin round-trip; the rest is local/DB work.
    log.info("[%s] TIMING force-close %s %s  total=%.0fms  "
             "[price@%.0f kucoin_close@%.0f done@%.0f]  closed=%d",
             user_id, mode, pair, _tmark.get("done", 0.0),
             _tmark.get("price", 0.0), _tmark.get("kucoin_close", 0.0),
             _tmark.get("done", 0.0), total_closed)
    return {
        "status":       "closed",
        "pair":         pair,
        "exit_price":   exit_price,
        "closed_count": total_closed,
        "pnl_abs":      round(total_pnl, 4),
    }


# ── Order Book (proxied from KuCoin, cached) ─────────────────────────────

@router.get("/orderbook/{symbol}")
async def futures_orderbook(
    symbol: str,
    user_id: str = Depends(get_user_id),
):
    """Get futures order book (20 levels) — cached 1.5s."""
    cache_key = f"ob:{symbol}"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < CACHE_TTL:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        data = await client.get_order_book(symbol)
        result = {"symbol": symbol, "asks": data.get("asks", []), "bids": data.get("bids", []), "ts": data.get("ts")}
    except Exception:
        from backend.services.kucoin_futures_client import generate_paper_orderbook
        pair = symbol.replace("USDTM", "/USDT").replace("-", "/")
        # Use futures perp price for paper orderbook generation so paper
        # and live use the same reference market.
        price = _futures_ticker_price(pair) or 50000
        ob = generate_paper_orderbook(price)
        result = {"symbol": symbol, "asks": ob["asks"], "bids": ob["bids"], "ts": ob["ts"]}

    _cache[cache_key] = (now, result)
    return result


@router.get("/trades/{symbol}")
async def futures_recent_trades(
    symbol: str,
    user_id: str = Depends(get_user_id),
):
    """Get recent futures trades for a symbol."""
    cache_key = f"rt:{symbol}"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < CACHE_TTL:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        trades = await client.get_recent_trades(symbol)
        result = {"symbol": symbol, "trades": trades[:50]}
    except Exception:
        result = {"symbol": symbol, "trades": []}

    _cache[cache_key] = (now, result)
    return result


@router.get("/contracts")
async def futures_contracts(
    user_id: str = Depends(get_user_id),
):
    """List available futures contracts."""
    cache_key = "contracts"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < 60:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        contracts = await client.get_contracts()
        result = {
            "contracts": [
                {
                    "symbol": c.get("symbol"),
                    "baseCurrency": c.get("baseCurrency"),
                    "multiplier": c.get("multiplier"),
                    "tickSize": c.get("tickSize"),
                    "lotSize": c.get("lotSize"),
                    "maxLeverage": c.get("maxLeverage"),
                    "isInverse": c.get("isInverse", False),
                    "status": c.get("status"),
                }
                for c in contracts
                if c.get("status") == "Open"
            ]
        }
    except Exception:
        result = {"contracts": []}

    _cache[cache_key] = (now, result)
    return result


# ── Pending Orders (limit/stop) ──────────────────────────────────────────

@router.post("/order")
def place_futures_order(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Place a futures order (limit, stop, etc.).
    In live mode, sends to KuCoin Lead Trading API."""
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    import time as _t

    # Frontend sends 'BTCUSDTM' but KuCoin Lead Trading expects 'XBTUSDTM'
    # (Bitcoin uses XBT, the original ISO 4217-style code, on KuCoin futures).
    # normalize_futures_symbol() handles the BTC→XBT swap and is a no-op for
    # symbols that are already correct.
    symbol     = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    side       = req.get("side", "buy")
    order_type = req.get("order_type", "limit")
    size       = float(req.get("size", 0))
    cost_usdt  = float(req.get("cost_usdt", 0))   # USDT cost sent by frontend
    price      = req.get("price")
    stop_price = req.get("stop_price")
    leverage   = req.get("leverage")
    tp_price   = req.get("tp_price")
    sl_price   = req.get("sl_price")
    hidden     = req.get("hidden", False)
    post_only  = req.get("post_only", False)
    reduce_only = req.get("reduce_only", False)
    time_in_force = req.get("time_in_force", "GTC")
    position_side = req.get("position_side")
    req_mode   = req.get("mode")  # explicit mode from frontend

    if size <= 0 and cost_usdt <= 0:
        return {"error": "size or cost_usdt must be positive"}

    eng = futures_engine_registry.for_user(user_id)

    if price is not None:
        price = float(price)
    if stop_price is not None:
        stop_price = float(stop_price)
    if leverage is not None:
        leverage = int(leverage)

    lev = min(LEAD_MAX_LEVERAGE, leverage or eng._leverage or 10)
    # Use explicit mode from request first, then engine mode, then default
    mode = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")

    # ── Recalculate size from cost_usdt when provided ────────────────────
    # The frontend sends cost_usdt (the USDT margin the user typed). For
    # live KuCoin we need an integer lot count and the *real* margin that
    # KuCoin will lock (1 lot is the minimum, so $1 BTC at 1x rounds up to
    # ~$79 — we must catch this and refuse with a clear error).
    real_margin = None
    real_notional = None
    if cost_usdt > 0:
        ref_price = price or stop_price
        if ref_price is None:
            # Use FUTURES price (was hitting spot — basis bug).
            pair_for_lookup = (
                symbol.replace("USDTM", "/USDT")
                      .replace("XBTUSDTM", "BTC/USDT")
                      .replace("-USDT", "/USDT")
            )
            ref_price = _futures_ticker_price(pair_for_lookup)

        if mode == "live":
            contracts, real_margin, real_notional, sz_err = _compute_live_sizing(
                cost_usdt=cost_usdt, leverage=lev,
                price=float(ref_price or 0), kc_symbol=symbol,
            )
            if sz_err:
                return {"error": sz_err}
            size = contracts
        else:
            # Paper mode: store USDT margin as the size unit (consistent with
            # manual-entry — Positions row shows the margin column).
            size = cost_usdt
            real_margin = cost_usdt
            real_notional = cost_usdt * lev

    # Determine position side
    if not position_side:
        if reduce_only:
            position_side = "SHORT" if side == "buy" else "LONG"
        else:
            position_side = "LONG" if side == "buy" else "SHORT"

    # ── Live mode: send to Lead Trading API ──────────────────────────────
    exchange_order_id = None
    if mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": err}
        try:
            client_oid = f"atf-ord-{int(_t.time()*1000)}"
            margin_mode = eng.get_symbol_margin(symbol).upper() or "ISOLATED"

            # Sync margin mode + leverage to KuCoin first — see comment in
            # /manual-entry for full rationale.
            sync_ok, sync_err = _sync_margin_mode_to_kucoin(
                eng, symbol, margin_mode, user_id
            )
            if not sync_ok:
                return {"error": sync_err}
            _sync_leverage_to_kucoin(eng, symbol, lev, user_id)

            # Skip the post-sync read-back — same reasoning as
            # /manual-entry above. Saves ~300ms per order. The reconcile
            # on /open every 30s will surface any KuCoin-side override.

            body: dict = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       symbol,
                "type":         order_type if order_type in ("market", "limit") else "limit",
                "size":         int(size),
                "leverage":     lev,
                "marginMode":   margin_mode,
                "positionSide": position_side,
            }
            if price is not None and order_type == "limit":
                body["price"] = str(price)
            if reduce_only:
                body["reduceOnly"] = True
            if time_in_force:
                body["timeInForce"] = time_in_force

            # Conditional / stop orders: place on the REGULAR Lead Trading
            # orders endpoint using the canonical stop-trigger fields.
            #
            # The old code POSTed to /api/v1/copy-trade/futures/st-orders with
            # triggerStopUpPrice / triggerStopDownPrice. KuCoin's Lead Trading
            # wrapper does NOT recognise those field names as a queued stop
            # trigger, so it executed the order IMMEDIATELY as a market order
            # the instant it was POSTed (and, because reduceOnly was force-set,
            # it could close an existing position). See the long history note in
            # /position/tp-sl. A real stop order needs:
            #     stop:          "up" | "down"  — trigger direction
            #     stopPrice:     "<price>"      — trigger price
            #     stopPriceType: "TP"           — Last Trade Price source
            # on /api/v1/copy-trade/futures/orders.
            if stop_price is not None or order_type in ("stop", "stop_limit"):
                # Trigger direction: "up" fires when the market rises THROUGH
                # the stop price, "down" when it falls THROUGH it. Derive it
                # from the stop price vs a reference price (the limit price the
                # user set, else the current market) so the trigger lands on the
                # correct side regardless of buy/sell. (The conditional and
                # trailing-stop UIs both send a limit `price`.)
                trig_ref = price
                if trig_ref is None:
                    pair_for_dir = (
                        symbol.replace("XBTUSDTM", "BTC/USDT")
                              .replace("USDTM", "/USDT")
                              .replace("-USDT", "/USDT")
                    )
                    trig_ref = _futures_ticker_price(pair_for_dir)
                if trig_ref:
                    body["stop"] = "up" if float(stop_price) >= float(trig_ref) else "down"
                else:
                    # Fallback: buy-stop = upward breakout, sell-stop = breakdown.
                    body["stop"] = "up" if side == "buy" else "down"
                body["stopPrice"]     = str(stop_price)
                body["stopPriceType"] = "TP"   # trigger from Last Trade Price
                # NOTE: do NOT force reduceOnly here — these are entry triggers.
                # reduceOnly is set above only when the caller explicitly
                # requested it.
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
            else:
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )

            if str(resp.get("code")) != "200000":
                return {"error": f"KuCoin Lead Trading: {resp.get('msg', resp)}"}
            exchange_order_id = resp.get("data", {}).get("orderId")
            log.info("[%s] Lead Trading order placed: %s", user_id, resp)
        except Exception as e:
            return {"error": f"Lead Trading order failed: {e}"}

    # Also track in engine (paper mode uses this for matching).
    # Pass `mode` so the manual-paper watchdog can filter out LIVE pending
    # orders — without it, live orders would be "filled" locally by
    # tick_pending_orders_paper, creating phantom paper positions for
    # orders KuCoin is independently working on.
    result = eng.place_pending_order(
        symbol=symbol, side=side, order_type=order_type, size=size,
        price=price, stop_price=stop_price, leverage=leverage,
        tp_price=float(tp_price) if tp_price else None,
        sl_price=float(sl_price) if sl_price else None,
        hidden=hidden, post_only=post_only, reduce_only=reduce_only,
        time_in_force=time_in_force,
        cost_usdt=cost_usdt,
        mode=mode,
    )

    # Persist to DB — include `mode` so paper limit orders don't leak into
    # the Live tab's Open Orders panel and vice versa.
    order_rec = FuturesOrder(
        user_id=user_id, mode=mode, symbol=symbol, side=side, order_type=order_type,
        size=size, price=price, stop_price=stop_price,
        leverage=lev, margin_mode="isolated",
        client_oid=result.get("order_id"), status="pending",
        exchange_order_id=exchange_order_id,
        time_in_force=time_in_force, hidden=hidden, post_only=post_only,
        reduce_only=reduce_only, tp_price=float(tp_price) if tp_price else None,
        sl_price=float(sl_price) if sl_price else None,
    )
    db.add(order_rec)
    db.commit()
    db.refresh(order_rec)
    result["db_id"] = order_rec.id
    result["exchange_order_id"] = exchange_order_id
    if real_margin is not None:
        result["margin"] = real_margin
    if real_notional is not None:
        result["notional"] = real_notional

    # ── Paper: immediate-fill check ────────────────────────────────────
    # The manual-paper watchdog runs every 5s. Without this immediate
    # check, a limit order placed at or beyond the current price (e.g.
    # buy limit at 69558 with current 69533) sits in Open Orders for
    # up to 5 seconds before the watchdog notices it should fill —
    # by which time the market may have moved past the limit. Calling
    # tick_pending_orders_paper synchronously here matches the order
    # against live price in the same request that placed it.
    optimistic_position_payload = None
    if mode == "paper":
        try:
            eng.tick_pending_orders_paper()
        except Exception as fill_exc:
            log.warning("[%s] Immediate-fill tick failed for %s: %s",
                        user_id, symbol, fill_exc)
        # Did the order disappear from pending? → it filled. Find the
        # resulting position and build an optimistic payload for the UI.
        client_oid_just_placed = result.get("order_id")
        order_still_pending = False
        with eng._lock:
            order_still_pending = client_oid_just_placed in eng._pending_orders
        if not order_still_pending:
            # Filled — find the position we just created so the frontend
            # can prepend it to the Positions tab (skipping the wait for
            # the next /open round-trip).
            with eng._lock:
                for trade_key, pos in eng.positions.items():
                    if (pos.pair.replace("/", "").replace("USDT", "USDTM") == symbol.upper()
                        and getattr(pos, "_mode", "paper") == "paper"):
                        # Latest position on this pair — likely ours.
                        cur = eng._get_live_price(pos.pair) or pos.entry
                        optimistic_position_payload = {
                            "id":                f"eng:{trade_key}",
                            "position_id":       f"eng:{trade_key}",
                            "_db_id":            getattr(pos, "db_id", None),
                            "pair":              pos.pair,
                            "side":              pos.direction,
                            "entry_price":       pos.entry,
                            "current_price":     cur,
                            "amount":            round(pos.size, 4),
                            "leverage":          getattr(pos, "leverage", 1),
                            "liquidation_price": getattr(pos, "liquidation_price", None),
                            "stoploss_price":    pos.sl,
                            "tp_price":          pos.tp,
                            "entry_time":        str(pos.opened_at),
                            "mode":              "paper",
                            "market_type":       "futures",
                            "unrealized_pnl":    0.0,
                            "source":            "manual",
                            "bot_key":           None,
                        }
                        break
            result["filled_immediately"] = True
            result["position"] = optimistic_position_payload

    # Also surface the pending-order payload so when the order DOESN'T
    # fill immediately, the frontend can prepend it to Open Orders
    # without waiting for /orders to round-trip.
    if optimistic_position_payload is None:
        result["order"] = {
            "order_id":    result.get("order_id"),
            "db_id":       order_rec.id,
            "symbol":      symbol,
            "side":        side,
            "order_type":  order_type,
            "size":        size,
            "price":       price,
            "stop_price":  stop_price,
            "leverage":    lev,
            "margin_mode": "isolated",
            "mode":        mode,
            "status":      "pending",
            "tp_price":    float(tp_price) if tp_price else None,
            "sl_price":    float(sl_price) if sl_price else None,
            "created_at":  str(order_rec.created_at),
        }

    log_event(db, user_id, "futures.place_order", request, payload=result)
    return result


@router.delete("/order/{order_id}")
def cancel_futures_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Cancel a pending futures order. In live mode, cancels on KuCoin Lead Trading.

    Supports two id formats:
      * Regular pending orders use `client_oid` (matches FuturesOrder.client_oid).
      * Advanced orders (TP/SL stops) returned by /orders use `stop:<exchange_id>`
        because they live only on KuCoin, not in our DB.
    """
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    import urllib.request, json as _json

    eng = futures_engine_registry.for_user(user_id)

    # ── Stop-order (Advanced Order) cancel branch ───────────────────────
    # Both Lead-Trading `/copy-trade/futures/orders/{id}` and regular
    # `/stopOrders/{id}` were returning 404 for the IDs we list from
    # `/api/v1/stopOrders`. KuCoin's own UI cancels these fine, which
    # means there's a route + ID combination that works — we just have
    # to find it.
    #
    # Strategy: look up the stop order's clientOid + symbol from the
    # active-stops list first, then attempt cancel against every known
    # endpoint variant in order of likelihood. The first 200000 wins.
    # Cancel-by-clientOid is more reliable than cancel-by-id because the
    # clientOid is the same regardless of which namespace the order
    # lives in.
    if order_id.startswith("stop:"):
        # Parse the encoded order_id. New format from /orders LIST is
        # "stop:{id}:{clientOid}:{symbol}". Legacy format (older client
        # builds) is "stop:{id}" — fall back to the DB+LIST lookup for
        # those.
        parts = order_id.split(":")
        stop_exchange_id = parts[1] if len(parts) > 1 else ""
        client_oid: str | None = parts[2] if len(parts) > 2 and parts[2] else None
        symbol: str | None     = parts[3] if len(parts) > 3 and parts[3] else None

        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": f"Cannot cancel stop order: {err}", "order_id": order_id}

        from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
        from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
        from backend.services.native_trading_engine import _kucoin_get_signed
        from urllib.parse import urlencode

        # Legacy path: if the encoded id didn't include clientOid/symbol,
        # fall back to DB + KuCoin /stopOrders LIST to find them.
        if not client_oid or not symbol:
            try:
                db_row = db.execute(
                    select(FuturesOrder.client_oid, FuturesOrder.symbol)
                    .where(
                        FuturesOrder.user_id == user_id,
                        FuturesOrder.exchange_order_id == stop_exchange_id,
                    )
                    .limit(1)
                ).first()
                if db_row:
                    client_oid = client_oid or db_row[0]
                    symbol     = symbol or db_row[1]
            except Exception as e:
                log.warning("[%s] DB clientOid lookup for stop %s failed: %s",
                            user_id, stop_exchange_id, e)
        if not client_oid or not symbol:
            try:
                list_resp = _kucoin_get_signed(
                    "/api/v1/stopOrders", eng._api_key, eng._api_sec, eng._api_pass,
                    params={"status": "active"}, base_url=_base,
                )
                for s in ((list_resp.get("data") or {}).get("items") or []):
                    if str(s.get("id")) == stop_exchange_id:
                        client_oid = client_oid or s.get("clientOid")
                        symbol     = symbol or s.get("symbol")
                        break
            except Exception as e:
                log.warning("[%s] Stop-order lookup before cancel failed: %s", user_id, e)

        log.info("[%s] stop-cancel start: id=%s clientOid=%s symbol=%s",
                 user_id, stop_exchange_id, client_oid, symbol)

        def _try_cancel(method: str, endpoint: str, query: dict | None = None):
            """Returns (ok, parsed_response_or_error_dict)."""
            try:
                ep = endpoint + (("?" + urlencode(query)) if query else "")
                ts = str(int(_time.time() * 1000))
                headers = _sign_request(
                    eng._api_sec, eng._api_pass, eng._api_key,
                    ts, method, ep,
                )
                url = f"{_base}{ep}"
                req_obj = urllib.request.Request(url, headers=headers, method=method)
                with _proxy_urlopen(req_obj, timeout=8) as resp:
                    return True, _json.loads(resp.read().decode())
            except urllib.error.HTTPError as he:
                try:
                    body = _json.loads(he.read().decode() or "{}")
                except Exception:
                    body = {}
                return False, {"http_status": he.code, "body": body}
            except Exception as e:
                return False, {"error": str(e)}

        # ── Documented KuCoin Lead Trading cancel endpoints ─────────────
        # Verified against https://www.kucoin.com/docs-new
        #   Copy Trading → Futures → Cancel Order By OrderId:
        #     DELETE /api/v1/copy-trade/futures/orders?orderId=X
        #     Permission: LeadtradeFutures ← user has this
        #     "Cancel an order (including a stop order)" — handles stops.
        #
        #   Copy Trading → Futures → Cancel Order By ClientOid:
        #     DELETE /api/v1/copy-trade/futures/orders/client-order
        #            ?symbol=X&clientOid=Y
        #     Permission: LeadtradeFutures ← user has this
        #
        # CRITICAL: every prior commit had orderId/clientOid in the URL
        # PATH. KuCoin's actual API expects them as QUERY parameters.
        # That's why every Lead Trading attempt returned 404 even though
        # the user's key has the right permission.
        attempts: list[tuple[str, str, dict | None]] = []

        # Primary: Lead Trading cancel-by-orderId (handles stop orders).
        if stop_exchange_id:
            attempts.append((
                "DELETE",
                "/api/v1/copy-trade/futures/orders",
                {"orderId": stop_exchange_id},
            ))
        # Secondary: Lead Trading cancel-by-clientOid (requires symbol).
        if client_oid and symbol:
            attempts.append((
                "DELETE",
                "/api/v1/copy-trade/futures/orders/client-order",
                {"symbol": symbol, "clientOid": client_oid},
            ))

        last_responses: list[tuple[str, dict | str]] = []
        for method, endpoint, query in attempts:
            ok_r, resp_r = _try_cancel(method, endpoint, query)
            last_responses.append((endpoint, resp_r))
            log.info("[%s] stop-cancel attempt %s -> ok=%s resp=%s",
                     user_id, endpoint, ok_r, resp_r)
            if ok_r and str((resp_r or {}).get("code", "")) == "200000":
                log_event(db, user_id, "futures.cancel_stop_order", request,
                          payload={"order_id": order_id, "endpoint": endpoint})
                log.info("[%s] Stop-order cancel ok via %s", user_id, endpoint)
                # Mark DB row as cancelled if we have one.
                try:
                    from sqlalchemy import update as sql_update
                    db.execute(
                        sql_update(FuturesOrder)
                        .where(
                            FuturesOrder.user_id == user_id,
                            FuturesOrder.exchange_order_id == stop_exchange_id,
                        )
                        .values(status="cancelled", cancelled_at=datetime.utcnow())
                    )
                    db.commit()
                except Exception:
                    db.rollback()
                return {"kucoin_cancelled": True, "order_id": order_id, "endpoint": endpoint}

        # Every attempt failed. Build a verbose diagnostic so we can SEE
        # which routes returned which errors. The alert shows just the
        # most informative message; the full breakdown is in `attempts`
        # and in the backend log.
        def _msg(r):
            if isinstance(r, dict):
                return (r.get("body") or {}).get("msg") or r.get("error") or r.get("msg")
            return str(r) if r else None
        non_404 = [(e, _msg(r)) for (e, r) in last_responses
                   if _msg(r) and "404" not in str(_msg(r)) and "Not Found" not in str(_msg(r))]
        raw_err = ""
        if non_404:
            err_endpoint, raw_err = non_404[0]
        else:
            raw_err = next((m for (_e, r) in last_responses if (m := _msg(r))), "unknown")

        # Dump the FULL per-attempt diagnostic into the error message so
        # we can see exactly which routes returned what. Helps pinpoint
        # the canonical endpoint without needing Railway log access.
        # Group identical responses to keep it compact.
        breakdown_lines: list[str] = []
        for (ep, r) in last_responses:
            msg = _msg(r)
            status = (r or {}).get("http_status") if isinstance(r, dict) else None
            code = (r or {}).get("code") if isinstance(r, dict) else None
            tag = f"[{status or code or '?'}]"
            breakdown_lines.append(f"  {tag} {ep} → {msg or 'no message'}")
        breakdown = "\n".join(breakdown_lines)
        err_str = (
            f"KuCoin rejected the cancel ({raw_err}).\n"
            f"client_oid={client_oid} symbol={symbol}\n"
            f"Per-attempt breakdown:\n{breakdown}"
        )

        log.warning("[%s] Stop-order cancel failed on all endpoints (client_oid=%s symbol=%s): %s",
                    user_id, client_oid, symbol, last_responses)
        return {
            "error": err_str,
            "order_id": order_id,
            "kucoin_cancelled": False,
            "client_oid": client_oid,
            "symbol": symbol,
            "raw_kucoin_error": raw_err,
            "attempts": [{"endpoint": e, "response": r} for (e, r) in last_responses],
        }

    # An order is "live" if it has an exchange_order_id (it was forwarded to
    # KuCoin Lead Trading). Engine mode is unreliable here because the user
    # may have placed the order in live mode then this request comes through
    # before any bot was ever started.
    #
    # Lookup falls back to the numeric DB id because the /orders LIST returns
    # `o.client_oid or str(o.id)`, and any row with a NULL client_oid was
    # uncancellable before — the previous query filtered on client_oid only,
    # the DB UPDATE further down did the same, so the cancel silently no-op'd
    # for legacy paper orders.
    db_order = db.execute(
        select(FuturesOrder).where(
            FuturesOrder.client_oid == order_id,
            FuturesOrder.user_id == user_id,
        )
    ).scalar_one_or_none()
    if db_order is None and order_id.isdigit():
        db_order = db.execute(
            select(FuturesOrder).where(
                FuturesOrder.id == int(order_id),
                FuturesOrder.user_id == user_id,
            )
        ).scalar_one_or_none()
    is_live_order = bool(db_order and db_order.exchange_order_id)

    # Track whether the KuCoin DELETE actually succeeded. Previously the
    # DB row was always marked `cancelled` regardless, so the UI showed
    # "cancelled" while the order was still alive on KuCoin — exactly the
    # bug the user reported. Now we only flip the DB status when KuCoin
    # confirms the cancel (or when the order is paper-only).
    kucoin_cancelled = not is_live_order   # paper orders need no exchange call
    kucoin_error: str | None = None
    if is_live_order:
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            log.warning("[%s] cancel-order skipped Lead Trading call: %s", user_id, err)
            kucoin_error = err
        else:
            try:
                # KuCoin Copy Trading docs:
                #   DELETE /api/v1/copy-trade/futures/orders?orderId=X
                #   Permission: LeadtradeFutures (user has it)
                # orderId is a QUERY parameter — not a path segment.
                # Same correction we made for stop-order cancels in
                # commit 9e7eb76.
                from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
                from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
                from urllib.parse import urlencode
                ts = str(int(_time.time() * 1000))
                qs = urlencode({"orderId": db_order.exchange_order_id})
                endpoint = f"/api/v1/copy-trade/futures/orders?{qs}"
                headers = _sign_request(
                    eng._api_sec, eng._api_pass, eng._api_key,
                    ts, "DELETE", endpoint,
                )
                url = f"{_base}{endpoint}"
                req_obj = urllib.request.Request(url, headers=headers, method="DELETE")
                with _proxy_urlopen(req_obj, timeout=8) as resp:
                    cancel_resp = _json.loads(resp.read().decode())
                code = str(cancel_resp.get("code", ""))
                if code == "200000":
                    kucoin_cancelled = True
                    log.info("[%s] Lead Trading cancel order ok: %s", user_id, cancel_resp)
                else:
                    kucoin_error = cancel_resp.get("msg") or f"KuCoin code {code}"
                    log.warning("[%s] Lead Trading cancel rejected: %s", user_id, cancel_resp)
            except Exception as e:
                kucoin_error = str(e)
                log.error("[%s] Lead Trading cancel failed: %s", user_id, e)

    # If the live cancel failed, surface the error and DON'T mark the row
    # cancelled — leaves it pending so the UI keeps showing it, the user
    # can retry, and KuCoin remains the source of truth.
    if is_live_order and not kucoin_cancelled:
        return {
            "error": (
                f"KuCoin Lead Trading could not cancel the order: {kucoin_error}. "
                "The order is still active on KuCoin — please retry or cancel "
                "directly from the KuCoin tab."
            ),
            "order_id": order_id,
            "kucoin_cancelled": False,
        }

    # If engine has the order, drop it. Use the actual client_oid from the
    # DB row when available so we hit the right key in pending_orders even
    # when the caller passed the numeric DB id.
    eng_key = (db_order.client_oid if db_order and db_order.client_oid else order_id)
    result = eng.cancel_pending_order(eng_key)
    # Don't treat "not found in engine" as an error for the user — engine
    # restarts wipe in-memory pending orders but the DB row is still the
    # source of truth, and the DB UPDATE below will mark it cancelled.
    if result and result.get("error", "").startswith("Order ") and "not found" in result.get("error", ""):
        result = {"cancelled": True, "order_id": eng_key, "engine_note": "not in engine memory (cleared on restart)"}

    from sqlalchemy import update as sql_update
    # Same fallback: when client_oid is NULL on the row, the WHERE never
    # matched and the row stayed pending forever. Match by id when present.
    if db_order is not None:
        upd_where = (FuturesOrder.id == db_order.id, FuturesOrder.user_id == user_id)
    else:
        upd_where = (FuturesOrder.client_oid == order_id, FuturesOrder.user_id == user_id)
    db.execute(
        sql_update(FuturesOrder)
        .where(*upd_where)
        .values(status="cancelled", cancelled_at=datetime.utcnow())
    )
    db.commit()

    log_event(db, user_id, "futures.cancel_order", request,
              payload={"order_id": order_id, "kucoin_cancelled": kucoin_cancelled})
    return {**(result or {}), "kucoin_cancelled": kucoin_cancelled, "order_id": order_id}


# ── Phase 6 — Cancel-all + partial-close endpoints ──────────────────────
#
# The user pointed out the app has single-order cancel but no cancel-all
# and no partial-close. These three endpoints fill that gap:
#
#   DELETE /api/futures/orders/all?mode=paper|live  → cancel every pending
#       order in that mode (DB rows + KuCoin cancellation for live).
#   DELETE /api/futures/orders/symbol/{symbol}?mode=...  → cancel everything
#       on one symbol/pair (used by the per-pair "Cancel All" button).
#   POST   /api/futures/position/partial-close      → close X% of an open
#       position via reduce-only market order. Used by the "Book 50% then
#       cancel rest" workflow the user described.
#
# Reuses the existing single-order cancel + KuCoin Lead Trading post path
# so error semantics are identical to the manual-trading flow.

@router.delete("/orders/all")
def cancel_all_futures_orders(
    request: Request,
    mode: str | None = None,
    symbol: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Cancel every pending futures order for this user, optionally filtered
    by `mode` ('paper'|'live') and/or `symbol`.

    Behaviour:
      • Paper orders: removed from the in-memory engine's pending-orders dict.
      • Live orders: a KuCoin Lead Trading DELETE is attempted for each;
        failures are reported per-order without aborting the whole batch.
      • DB rows: marked cancelled with timestamp.

    Returns a summary `{cancelled: N, failed: [...], kucoin_attempted: N}`.
    """
    # Fetch every pending order matching the filters
    q = select(FuturesOrder).where(
        FuturesOrder.user_id == user_id,
        FuturesOrder.status == "pending",
    )
    if mode in ("paper", "live"):
        q = q.where(FuturesOrder.mode == mode)
    if symbol:
        # Match KuCoin (BTCUSDTM) and slash (BTC/USDT) variants
        from sqlalchemy import or_ as _or
        sym_variants = [symbol, symbol.replace("/", ""), symbol.replace("/", "").replace("USDT", "USDTM")]
        q = q.where(_or(*[FuturesOrder.symbol == s for s in sym_variants]))

    orders = db.execute(q).scalars().all()
    if not orders:
        return {"cancelled": 0, "failed": [], "kucoin_attempted": 0, "message": "No pending orders match the filter"}

    eng = futures_engine_registry.for_user(user_id)
    now = datetime.utcnow()
    cancelled = 0
    failed: list[dict] = []
    kucoin_attempted = 0

    # Load live credentials once if the batch contains any LIVE orders — the
    # per-order cancel needs eng._api_key populated. Previously this relied on
    # eng._api_key already being set; when it wasn't (no live bot started this
    # session) the live DELETE was silently skipped yet the row was still
    # marked cancelled, leaving a real order alive on KuCoin but "cancelled"
    # in the UI.
    has_live = any(o.mode == "live" and o.exchange_order_id for o in orders)
    live_creds_ok = True
    if has_live:
        live_creds_ok, _creds_err = _ensure_live_credentials(eng, user_id, db)

    for o in orders:
        # For LIVE orders, attempt the KuCoin cancel FIRST and finalize the DB
        # row only when KuCoin confirms (code 200000). Marking the row
        # cancelled before/without confirmation showed the user "cancelled"
        # while the order was still live on KuCoin — the same bug the
        # single-order cancel route fixes. Failed cancels stay 'pending' so
        # the user can retry and KuCoin stays the source of truth.
        if o.mode == "live" and o.exchange_order_id:
            kucoin_attempted += 1
            if not live_creds_ok or not eng._api_key:
                failed.append({
                    "order_id": o.id, "exchange_id": o.exchange_order_id,
                    "error": "Live credentials unavailable — cannot cancel on KuCoin",
                })
                continue
            try:
                import urllib.request, json as _json
                from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
                from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
                from urllib.parse import urlencode
                ts = str(int(_time.time() * 1000))
                qs = urlencode({"orderId": o.exchange_order_id})
                endpoint = f"/api/v1/copy-trade/futures/orders?{qs}"
                headers = _sign_request(
                    eng._api_sec, eng._api_pass, eng._api_key,
                    ts, "DELETE", endpoint,
                )
                url = f"{_base}{endpoint}"
                req_obj = urllib.request.Request(url, headers=headers, method="DELETE")
                with _proxy_urlopen(req_obj, timeout=8) as resp:
                    cancel_resp = _json.loads(resp.read().decode())
                code = str(cancel_resp.get("code", ""))
                if code != "200000":
                    failed.append({
                        "order_id": o.id, "exchange_id": o.exchange_order_id,
                        "error": cancel_resp.get("msg") or f"KuCoin code {code}",
                    })
                    continue   # leave row pending — don't count as cancelled
            except Exception as e:
                failed.append({"order_id": o.id, "exchange_id": o.exchange_order_id, "error": str(e)})
                continue

        # Paper order, or live cancel confirmed → finalize the row + engine.
        o.status = "cancelled"
        o.cancelled_at = now
        try:
            eng.cancel_pending_order(o.client_oid or f"db-{o.id}")
        except Exception:
            pass
        cancelled += 1

    db.commit()
    log_event(db, user_id, "futures.cancel_all_orders", request,
              payload={"mode": mode, "symbol": symbol, "cancelled": cancelled,
                       "failed": len(failed)})
    return {
        "cancelled":        cancelled,
        "failed":           failed,
        "kucoin_attempted": kucoin_attempted,
        "mode":             mode,
        "symbol":           symbol,
    }


@router.post("/position/partial-close")
def partial_close_futures_position(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Close a configurable percentage of an open futures position.

    Body:
      {
        "pair":      "BTC/USDT",       # required
        "mode":      "paper" | "live", # required
        "close_pct": 50,               # 1..99 (percentage of position to close)
      }

    Workflow:
      1. Locate the open position on the per-user engine (paper) or the
         per-user/per-bot engine (when bot_id provided).
      2. PAPER: book partial P&L into engine.balance, reduce position
         remaining_pct, update closed_trades audit, return the booking.
      3. LIVE: send a reduce-only market order to KuCoin Lead Trading
         for close_pct of the position's contract count. Local position
         is updated to reflect the reduction.

    Used by the bot panel's "Book 50%" button and by ARM TP1 booking
    (the engine calls this internally too in Phase 3).
    """
    # ── Latency instrumentation (see /manual-entry for rationale) ───────
    _t0 = _time.perf_counter()
    _tmark: dict[str, float] = {}
    def _ck(_name: str) -> None:
        _tmark[_name] = (_time.perf_counter() - _t0) * 1000.0

    pair      = req.get("pair")
    mode      = req.get("mode", "paper")
    close_pct = float(req.get("close_pct", 50))
    # Optional direction so hedge-mode (long + short on the same pair) and
    # the KuCoin-only fallback below act on the correct leg.
    direction_req = req.get("direction")
    if direction_req not in ("long", "short"):
        direction_req = None
    # Optional unique row id so 25/50/75% targets the specific position
    # the user clicked instead of "first match on this pair".
    req_position_id = req.get("position_id")
    target_eng_key: str | None = None
    target_db_id: int | None = None
    if isinstance(req_position_id, str) and req_position_id:
        if req_position_id.startswith("eng:"):
            target_eng_key = req_position_id[4:]
        elif req_position_id.startswith("db:"):
            try:
                target_db_id = int(req_position_id[3:])
            except ValueError:
                target_db_id = None
    if not pair:
        return {"error": "pair is required"}
    if not (0.5 <= close_pct <= 99.5):
        return {"error": "close_pct must be between 0.5 and 99.5"}
    if mode not in ("paper", "live"):
        return {"error": "mode must be 'paper' or 'live'"}

    # Search BOTH the main user engine AND all per-bot engines for the
    # position. The old code only checked the main engine, which fails
    # when the position belongs to a per-bot engine (the multi-bot
    # architecture introduced after the main-engine refactor). Without
    # this, the 25%/50%/75% partial-close buttons on the Positions panel
    # always errored 'No open position' for any bot-owned position.
    candidate_engines = []
    main_eng = futures_engine_registry.for_user(user_id)
    if main_eng is not None:
        candidate_engines.append(main_eng)
    try:
        for _key, _eng in futures_engine_registry.user_bot_engines(user_id):
            if _eng is not None:
                candidate_engines.append(_eng)
    except Exception:
        pass

    eng = None
    pos = None
    trade_key = None
    for _eng in candidate_engines:
        for k, p in _eng.positions.items():
            if p.pair != pair:
                continue
            # Filter by POSITION mode tag, not engine mode. The main user
            # engine defaults to _mode='paper' even when it holds a
            # manual LIVE position (tagged at /manual-entry). Filtering
            # by engine mode would skip the main engine for live mode
            # and fail to find live manual positions.
            pos_mode = getattr(p, "_mode", getattr(_eng, "_mode", "paper"))
            if pos_mode != mode:
                continue
            # Hedge-mode: when a direction is supplied, only match that leg
            # (no-op when direction wasn't sent, preserving legacy behaviour).
            if direction_req is not None and p.direction != direction_req:
                continue
            # When the caller targets a specific engine row, accept only
            # that exact trade_key. When they target a DB row (or no
            # specific id was passed), behave as before.
            if target_eng_key is not None and k != target_eng_key:
                continue
            if target_db_id is not None:
                # User wants the DB-fallback orphan path — don't match
                # any engine position so we fall through to that branch.
                continue
            eng = _eng
            pos = p
            trade_key = k
            break
        if pos is not None:
            break

    if pos is None:
        # ── Orphan-DB fallback ─────────────────────────────────────────
        # Position not in any running engine, but might be an ORPHAN row
        # in the trades table (engine was killed/restarted leaving the
        # Trade row marked status='open' with no engine to manage it).
        #
        # When the caller targets a specific row (target_db_id), use that
        # exact row — the user clicked 25/50/75% on that row and expects
        # only that one to be affected. Otherwise fall back to "most
        # recent orphan on this pair", which is the legacy behaviour.
        orphan_q = select(Trade).where(
            Trade.user_id    == user_id,
            Trade.pair       == pair,
            Trade.market_type == "futures",
            Trade.mode       == mode,
            Trade.status     == "open",
        )
        if direction_req:
            orphan_q = orphan_q.where(Trade.side == direction_req)
        if target_db_id is not None:
            orphan_q = orphan_q.where(Trade.id == target_db_id)
        else:
            orphan_q = orphan_q.order_by(desc(Trade.entry_time)).limit(1)
        orphan = db.execute(orphan_q).scalar_one_or_none()
        if orphan is None:
            # ── KuCoin-direct fallback (LIVE only) ──────────────────────
            # Same rationale as add-margin: a live position can exist ONLY on
            # KuCoin (engine state lost on a restart, opened in KuCoin's own
            # UI, or a filled limit order). /open shows it as a 'kucoin-…'
            # row with no engine or DB binding, so the 25/50/75% buttons
            # errored 'No open position'. Send a reduce-only market order to
            # KuCoin for close_pct of the live position's contracts.
            if mode == "live":
                kc_eng = futures_engine_registry.for_user(user_id)
                ok_creds, _cerr = _ensure_live_credentials(kc_eng, user_id, db)
                if ok_creds:
                    kc_pos = _fetch_kucoin_live_position(kc_eng, pair, direction_req)
                    if kc_pos is not None:
                        exit_p = _futures_ticker_price(pair) or kc_pos["entry"]
                        class _KcPos:
                            pass
                        _kp = _KcPos()
                        _kp.pair          = pair
                        _kp.direction     = kc_pos["direction"]
                        _kp.entry         = kc_pos["entry"]
                        _kp.size          = kc_pos["margin"]
                        _kp.leverage      = int(kc_pos["leverage"]) or 1
                        _kp.tp2_price     = None
                        _kp.sl            = 0
                        _kp.remaining_pct = 1.0
                        # CRITICAL: tag _mode='live' so _live_order_allowed
                        # permits the order on the paper-default main engine
                        # (otherwise _place_live_partial_close silently no-ops).
                        _kp._mode         = "live"
                        ok_live, err_live = kc_eng._place_live_partial_close(
                            pair, _kp, close_pct / 100.0, exit_p,
                        )
                        if not ok_live:
                            return {"error": f"KuCoin rejected partial close: {err_live}"}
                        return {
                            "ok": True, "pair": pair, "mode": "live",
                            "fill_price": exit_p, "close_pct": close_pct,
                            "leg_pnl": 0.0,
                            "remaining_pct": round((1.0 - close_pct / 100.0) * 100.0, 2),
                            "fully_closed": close_pct >= 99.0,
                            "source": "kucoin_live",
                        }
            return {"error": f"No open position for {pair} in {mode} mode"}

        # ── Actually do a PARTIAL close on the DB row ─────────────────
        # Previously this branch did a forced FULL close regardless of
        # close_pct — which is exactly the "25% closes the whole
        # position" bug the user flagged. The Trade row doesn't have a
        # remaining_pct column, but we can simulate the partial by
        # reducing `amount` (= current margin) and crediting the booked
        # leg P&L into profit_abs (additive). Position stays status=open
        # with the smaller margin until it's fully closed.
        from datetime import timezone as _tz
        exit_p = _futures_ticker_price(pair) or orphan.entry_price
        now_dt = datetime.now(_tz.utc)
        side = getattr(orphan, "side", "long") or "long"
        lev = orphan.leverage or 1
        if side == "short":
            leg_pct = (orphan.entry_price - exit_p) / orphan.entry_price * 100 * lev
        else:
            leg_pct = (exit_p - orphan.entry_price) / orphan.entry_price * 100 * lev
        close_fraction = close_pct / 100.0
        prev_amount = float(orphan.amount or 0)
        leg_margin = prev_amount * close_fraction
        leg_pnl = leg_margin * (leg_pct / 100.0)
        new_amount = prev_amount - leg_margin

        # ── LIVE orphan: also hit KuCoin so on-exchange reflects the
        #   reduction. Without this, the DB shrinks but KuCoin still
        #   holds the original size — the next /open reconcile would
        #   either resurrect the original size or report a phantom.
        if mode == "live":
            try:
                main_eng = futures_engine_registry.for_user(user_id)
                ok_creds, err_creds = _ensure_live_credentials(main_eng, user_id, db)
                if not ok_creds:
                    return {
                        "error": (f"Cannot partial-close live orphan: {err_creds}. "
                                  "DB unchanged."),
                    }
                # Build a minimal pos-like object so we can reuse
                # _place_live_partial_close. Shrinking by close_fraction
                # of current (orphan.amount). The helper applies this
                # fraction to its OWN contract calc using pos.size +
                # pos.leverage, so feed the current effective size.
                class _OrphanPos:
                    pass
                _op = _OrphanPos()
                _op.pair      = pair
                _op.direction = side
                _op.entry     = orphan.entry_price
                _op.size      = prev_amount        # current margin (already-shrunk)
                _op.leverage  = lev
                _op.tp2_price = None
                _op.sl        = orphan.stoploss_price or 0
                _op.remaining_pct = 1.0            # full of CURRENT for the helper's clamp
                # CRITICAL: tag _mode='live' so _live_order_allowed permits the
                # order on the paper-default main engine — without it
                # _place_live_partial_close returns (True, "") WITHOUT hitting
                # KuCoin, silently shrinking the DB row while the real position
                # keeps its full size on the exchange.
                _op._mode     = "live"
                ok_live, err_live = main_eng._place_live_partial_close(
                    pair, _op, close_fraction, exit_p,
                )
                if not ok_live:
                    return {
                        "error": (f"KuCoin rejected partial close: {err_live}. "
                                  "DB unchanged — retry or close from KuCoin tab."),
                    }
            except Exception as live_exc:
                return {
                    "error": (f"KuCoin partial-close on orphan failed: {live_exc}. "
                              "DB unchanged."),
                }

        is_full_close = new_amount <= 0.01
        if is_full_close:
            # Effectively done — close the row.
            orphan.exit_price  = exit_p
            orphan.exit_time   = now_dt
            orphan.exit_reason = "manual_partial_close_full"
            orphan.status      = "closed"
            orphan.profit_pct  = round(leg_pct, 4)
            orphan.profit_abs  = round((orphan.profit_abs or 0.0) + leg_pnl, 4)
        else:
            # True partial: shrink amount, accumulate booked P&L on the
            # row. status stays 'open' so the row keeps showing.
            orphan.amount     = round(new_amount, 8)
            orphan.profit_abs = round((orphan.profit_abs or 0.0) + leg_pnl, 4)

        # Credit the user's main paper engine balance with the leg P&L.
        # Without this the user "books" partial profit in the orphan row
        # but their visible Asset Overview never updates. Live skips —
        # KuCoin's own balance is the source of truth.
        if mode == "paper":
            try:
                _eng = futures_engine_registry.for_user(user_id)
                if _eng is not None:
                    with _eng._lock:
                        _eng.balance = float(_eng.balance or 0.0) + leg_pnl
            except Exception:
                pass

        db.commit()
        return {
            "ok":            True,
            "pair":          pair,
            "mode":          mode,
            "fill_price":    exit_p,
            "close_pct":     close_pct,
            "leg_pnl":       round(leg_pnl, 4),
            "remaining_pct": round((new_amount / max(prev_amount, 0.0001)) * 100.0, 2),
            "fully_closed":  is_full_close,
            "trade_id":      orphan.id,
            "source":        "db_orphan",
        }

    close_fraction = close_pct / 100.0
    # Use the engine's last-known price as the partial-fill reference.
    # In live mode this is updated every tick from KuCoin; in paper mode
    # it tracks the same market data so paper exits match live timing.
    fill_price = eng._get_live_price(pair) or pos.entry
    _ck("price")
    from datetime import timezone as _tz
    now = datetime.now(_tz.utc)

    # Initialise partial-close state on first use.
    if not hasattr(pos, "partial_pnl_abs"):
        pos.partial_pnl_abs = 0.0
    if not hasattr(pos, "partial_exits"):
        pos.partial_exits = []
    if not hasattr(pos, "remaining_pct"):
        pos.remaining_pct = 1.0

    # CRITICAL: close_pct is applied to the CURRENT REMAINING size, not
    # the original. This matches KuCoin's UI semantics ("25% closes 25%
    # of what's open") and the optimistic-UI math in PositionsPanel
    # (which multiplies the displayed `amount` by `1 - fraction`).
    # Previously close_fraction was applied to pos.size (original
    # margin), so re-clicking 25% on an already-shrunken row gave
    # different results between frontend and backend on the refresh.
    prev_remaining = float(pos.remaining_pct or 1.0)
    leg_remaining_fraction  = prev_remaining * close_fraction        # fraction of ORIGINAL being closed this leg
    new_remaining           = prev_remaining * (1.0 - close_fraction)
    leg_margin              = pos.size * leg_remaining_fraction

    if pos.direction == "long":
        raw_pct = (fill_price - pos.entry) / pos.entry
    else:
        raw_pct = (pos.entry - fill_price) / pos.entry
    leveraged_pct = raw_pct * getattr(pos, "leverage", 1)
    leg_pnl = leg_margin * leveraged_pct

    # ── LIVE first: hit KuCoin BEFORE we mutate local state so a
    #   rejection leaves us with the SAME position state we already
    #   showed the user (no phantom local update). Paper mode skips.
    if mode == "live":
        try:
            ok_live, err_live = eng._place_live_partial_close(
                pair, pos, leg_remaining_fraction, fill_price,
            )
            if not ok_live:
                log_event(db, user_id, "futures.partial_close.live_failed", request,
                          payload={"pair": pair, "close_pct": close_pct, "error": err_live})
                return {
                    "ok":    False,
                    "error": (
                        f"KuCoin Lead Trading rejected the partial close: {err_live}. "
                        "Position state unchanged — retry or close from the KuCoin tab."
                    ),
                }
        except Exception as e:
            log_event(db, user_id, "futures.partial_close.live_failed", request,
                      payload={"pair": pair, "close_pct": close_pct, "error": str(e)})
            return {
                "ok":    False,
                "error": f"KuCoin partial-close call failed: {e}. Position unchanged.",
            }
        _ck("kucoin_close")

    # Mutate local state AFTER KuCoin confirms (or immediately for paper).
    pos.partial_pnl_abs += leg_pnl
    pos.remaining_pct    = max(0.0, new_remaining)
    pos.partial_exits.append({
        "ts":         now.isoformat(),
        "price":      round(float(fill_price), 6),
        "reason":     "manual_partial_close",
        "close_pct":  round(close_fraction, 4),       # what the USER clicked (25/50/75)
        "leg_fraction_of_original": round(leg_remaining_fraction, 4),
        "pnl_abs":    round(float(leg_pnl), 4),
    })
    # Credit the wallet only when the position's mode matches this engine's
    # wallet mode. The shared manual engine is paper-mode, so a LIVE manual
    # partial must NOT pollute the paper wallet (KuCoin owns live equity).
    # A live BOT engine (_mode="live") DOES track its own live P&L locally,
    # matching the engine's _tick close paths — using `== eng._mode` (rather
    # than a literal "paper") keeps the partial credit consistent with the
    # final-close subtraction of partial_pnl_abs, so the leg is counted once.
    eng_mode = getattr(eng, "_mode", "paper")
    pos_mode = getattr(pos, "_mode", eng_mode)
    if pos_mode == eng_mode:
        eng.balance += leg_pnl

    # ── Persist partial close as its own Trade row so it shows in
    #   Position History. Without this, the user clicks 25/50/75 and the
    #   booked P&L is invisible in History — only the FINAL close lands
    #   there. This gives a complete audit trail: every booking event
    #   becomes a row tagged "manual_partial_<pct>%". The leg P&L is
    #   the realised profit on the closed portion.
    try:
        from backend.models.trade import Trade as TradeModel
        from backend.models.database import SessionLocal as _SL
        with _SL() as _db:
            _db.add(TradeModel(
                user_id     = user_id,
                mode        = pos_mode,
                market_type = "futures",
                pair        = pair,
                side        = pos.direction,
                leverage    = getattr(pos, "leverage", 1),
                entry_price = round(pos.entry, 8),
                exit_price  = round(float(fill_price), 8),
                amount      = round(leg_margin, 8),
                profit_pct  = round(leveraged_pct * 100.0, 4),
                profit_abs  = round(float(leg_pnl), 4),
                stoploss_price = round(pos.sl, 8) if getattr(pos, "sl", None) else None,
                entry_time  = pos.opened_at,
                exit_time   = now,
                exit_reason = f"manual_partial_{int(close_pct)}pct",
                status      = "closed",
                strategy_id = eng._strategy_id,
            ))
            _db.commit()
    except Exception as persist_exc:
        log.warning("[%s] Failed to persist partial-close history row: %s",
                    user_id, persist_exc)

    # If close_pct = ~100, the position is effectively flat — auto-close it.
    if pos.remaining_pct <= 0.01:
        pos.close(fill_price, "manual_full_close", now)
        eng.closed_trades.append(pos)
        del eng.positions[trade_key]
        # Anti-double-count: we just wrote a partial Trade row for the
        # final leg above. pos.pnl_abs from pos.close() includes the
        # SUM of all partials too (because Position.close() does
        # final_leg_pnl + partial_pnl_abs). If we let _persist_closed_trade
        # use pos.pnl_abs, the original open Trade row would close with
        # the full round-trip P&L while the partial rows ALSO carry their
        # leg P&L — totals double-count. Override pos.pnl_abs with just
        # the FINAL leg so the original row's contribution is exactly
        # the last leg, and the sum of all closed rows = total realised.
        _saved_pnl = pos.pnl_abs
        try:
            final_leg_only = pos.pnl_abs - getattr(pos, "partial_pnl_abs", 0.0)
            pos.pnl_abs = final_leg_only
            from backend.services.native_trading_engine import _persist_closed_trade
            _persist_closed_trade(user_id, pos, mode, eng._strategy_id, pos.db_id)
        finally:
            pos.pnl_abs = _saved_pnl

    log_event(db, user_id, "futures.partial_close", request,
              payload={"pair": pair, "mode": mode, "close_pct": close_pct,
                       "leg_pnl": round(leg_pnl, 4),
                       "remaining_pct": round(pos.remaining_pct, 4)})
    _ck("done")
    # Cumulative ms-since-click: price=fill price ready, kucoin_close=KuCoin
    # reduce-only partial confirmed (live only), done=response built (history
    # row + log_event commits included).
    log.info("[%s] TIMING partial-close %s %s %.0f%%  total=%.0fms  "
             "[price@%.0f kucoin_close@%.0f done@%.0f]",
             user_id, mode, pair, close_pct, _tmark.get("done", 0.0),
             _tmark.get("price", 0.0), _tmark.get("kucoin_close", 0.0),
             _tmark.get("done", 0.0))
    return {
        "ok":            True,
        "pair":          pair,
        "mode":          mode,
        "fill_price":    round(float(fill_price), 6),
        "close_pct":     close_pct,
        "leg_pnl":       round(leg_pnl, 4),
        "remaining_pct": round(pos.remaining_pct, 4),
        "fully_closed":  pos.remaining_pct <= 0.01,
    }


@router.get("/risk-config")
def get_risk_config(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """NICE-4 (FR-04) — return the per-TF risk overrides for this user.

    Response shape:
      {
        defaults: {"1m": {atr_period, sl_mult, tp_mult, min_rr}, ...},
        overrides: {"1m": {...}, "1h": {...}}    # may be empty
      }

    The UI renders the defaults next to the overrides so the user always
    sees the baseline. PUT writes overrides only (delete a key to revert
    that TF to default).
    """
    import json as _json
    from backend.services.risk_engine import TIMEFRAME_CONFIG
    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    overrides: dict = {}
    if cfg and getattr(cfg, "risk_config_json", None):
        try:
            parsed = _json.loads(cfg.risk_config_json)
            if isinstance(parsed, dict):
                overrides = parsed
        except Exception:
            overrides = {}
    # Defaults — sanitised to the same shape the UI will write back.
    defaults = {
        tf: {
            "atr_period": int(c["atr_period"]),
            "sl_mult":    float(c["sl_mult"]),
            "tp_mult":    float(c["tp_mult"]),
            "min_rr":     float(c["min_rr"]),
            "style":      c.get("style", ""),
        } for tf, c in TIMEFRAME_CONFIG.items()
    }
    return {"defaults": defaults, "overrides": overrides}


@router.put("/risk-config")
def put_risk_config(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Write per-TF overrides. Body:
      {"overrides": {"1m": {sl_mult: 1.2, tp_mult: 2.5}, "1h": {min_rr: 2.5}}}

    Only the keys provided are stored. Missing keys (e.g. atr_period) fall
    back to TIMEFRAME_CONFIG defaults at risk_engine.compute_tp_sl time.

    Validation:
      • TF must be in {1m, 5m, 15m, 30m, 1h, 4h, 1d}
      • sl_mult / tp_mult clamped to (0.1, 10.0)
      • min_rr clamped to (1.0, 5.0)
      • atr_period clamped to (5, 50)
    """
    import json as _json
    from backend.services.risk_engine import TIMEFRAME_CONFIG, invalidate_overrides_cache

    raw = req.get("overrides") or {}
    if not isinstance(raw, dict):
        return {"error": "overrides must be an object"}

    VALID_TFS = set(TIMEFRAME_CONFIG.keys())
    cleaned: dict = {}
    for tf, vals in raw.items():
        if tf not in VALID_TFS or not isinstance(vals, dict):
            continue
        tf_block: dict = {}
        if "atr_period" in vals and vals["atr_period"] is not None:
            try:
                tf_block["atr_period"] = max(5, min(50, int(vals["atr_period"])))
            except (TypeError, ValueError):
                pass
        for k in ("sl_mult", "tp_mult"):
            if k in vals and vals[k] is not None:
                try:
                    tf_block[k] = max(0.1, min(10.0, float(vals[k])))
                except (TypeError, ValueError):
                    pass
        if "min_rr" in vals and vals["min_rr"] is not None:
            try:
                tf_block["min_rr"] = max(1.0, min(5.0, float(vals["min_rr"])))
            except (TypeError, ValueError):
                pass
        if tf_block:
            cleaned[tf] = tf_block

    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        cfg = Config(user_id=user_id)
        db.add(cfg)
    cfg.risk_config_json = _json.dumps(cleaned) if cleaned else None
    db.commit()
    # Drop the per-user cached overrides so the very next signal scan
    # picks up the new values.
    invalidate_overrides_cache(user_id)

    log_event(db, user_id, "futures.risk_config.put", request, payload={
        "tfs_set": list(cleaned.keys()),
    })
    return {"ok": True, "overrides": cleaned}


@router.get("/orders")
def get_futures_orders(
    symbol: str = None,
    status: str = "pending",
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get pending or filled futures orders, filtered by mode for strict
    paper/live isolation. The Live Open Orders tab passes mode=live so paper
    limit orders never leak in, and vice versa.

    For live mode, also reconciles DB rows against KuCoin's actual order
    status — limit orders that filled (e.g. buy-above-market) get flipped
    to 'filled' here so they stop showing in the Open Orders tab while a
    real position is open."""
    eng = futures_engine_registry.for_user(user_id)
    # NOTE: `eng.get_pending_orders()` is intentionally NOT used here.
    # Engine in-memory pending orders are kept for the paper-mode matching
    # loop, but the API responds from the FuturesOrder DB rows so the
    # `mode` column drives strict paper/live separation. See the long
    # comment near the return statement below.

    # ── Live-mode reconcile: ask KuCoin if our 'pending' LIVE rows really
    # are still pending. KuCoin only returns truly-active orders, so any of
    # our rows with an exchange_order_id NOT in that list has either filled
    # or been cancelled. Paper orders never had an exchange_order_id so
    # they're naturally excluded from this reconcile.
    if (status == "pending" or status is None) and mode != "paper" and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            kc_resp = _kucoin_get_signed(
                "/api/v1/orders", eng._api_key, eng._api_sec, eng._api_pass,
                params={"status": "active"},
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(kc_resp.get("code")) == "200000":
                items = (kc_resp.get("data") or {}).get("items") or []
                live_active_ids = {str(o.get("id")) for o in items if o.get("id")}
                pending_rows = db.execute(
                    select(FuturesOrder).where(
                        FuturesOrder.user_id == user_id,
                        FuturesOrder.status == "pending",
                        FuturesOrder.mode == "live",
                        FuturesOrder.exchange_order_id.isnot(None),
                    )
                ).scalars().all()
                changed = False
                for o in pending_rows:
                    if str(o.exchange_order_id) in live_active_ids:
                        continue
                    # Disappeared from active list. Could mean FILLED or
                    # CANCELLED — previously we always marked "filled"
                    # which was wrong for orders the user cancelled on
                    # KuCoin's own UI (they'd show as Filled in our
                    # history with no exit). Fetch the order's actual
                    # final state and use that.
                    new_status = "filled"   # default fallback
                    try:
                        detail = _kucoin_get_signed(
                            f"/api/v1/orders/{o.exchange_order_id}",
                            eng._api_key, eng._api_sec, eng._api_pass,
                            base_url=KUCOIN_FUTURES_BASE,
                        )
                        if str(detail.get("code")) == "200000":
                            d = detail.get("data") or {}
                            deal_size = float(d.get("dealSize") or 0)
                            is_cancelled = bool(d.get("cancelExist"))
                            if is_cancelled and deal_size <= 0:
                                new_status = "cancelled"
                            elif deal_size > 0:
                                new_status = "filled"
                            else:
                                # Edge case — order gone, no fill, not
                                # explicitly cancelled. Treat as cancelled
                                # so we don't claim a phantom fill.
                                new_status = "cancelled"
                    except Exception as detail_exc:
                        log.debug("[%s] order-detail lookup for %s failed: %s "
                                  "(defaulting to filled)",
                                  user_id, o.exchange_order_id, detail_exc)
                    o.status = new_status
                    if new_status == "filled":
                        o.filled_at = datetime.utcnow()
                    else:
                        o.cancelled_at = datetime.utcnow()
                    changed = True
                    log.info("[%s] Reconcile: order %s no longer active "
                             "→ marked %s", user_id, o.exchange_order_id, new_status)
                if changed:
                    db.commit()
        except Exception as e:
            log.warning("[%s] KuCoin order reconcile failed: %s", user_id, e)

    # ── Live-mode: also fetch KuCoin Lead Trading "Advanced Orders" (stop
    # orders, i.e. attached TP/SL). These don't live in our DB because they
    # were placed directly via /st-orders against KuCoin and KuCoin is the
    # source of truth. We merge them into the response so the Open Orders
    # tab can show what KuCoin's "Advanced Orders" sub-tab shows.
    stop_orders: list[dict] = []
    if (status == "pending" or status is None) and mode != "paper" and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            params = {"status": "active"}
            if symbol:
                params["symbol"] = symbol
            so_resp = _kucoin_get_signed(
                "/api/v1/stopOrders", eng._api_key, eng._api_sec, eng._api_pass,
                params=params, base_url=KUCOIN_FUTURES_BASE,
            )
            if str(so_resp.get("code")) == "200000":
                items = (so_resp.get("data") or {}).get("items") or []
                # Build a clientOid → cancel-compatible order_id lookup. We
                # first try our DB (orders we POSTed get their KuCoin orderId
                # stored at placement time). For any stop we don't have a DB
                # row for (placed by an earlier build, or restored from a
                # KuCoin-only state) we fall back to KuCoin's byClientOid
                # endpoint to fetch the canonical id and persist it.
                client_oids = [s.get("clientOid") for s in items if s.get("clientOid")]
                lead_id_by_oid: dict[str, str] = {}
                if client_oids:
                    rows = db.execute(
                        select(FuturesOrder.client_oid, FuturesOrder.exchange_order_id)
                        .where(
                            FuturesOrder.user_id == user_id,
                            FuturesOrder.client_oid.in_(client_oids),
                            FuturesOrder.exchange_order_id.isnot(None),
                        )
                    ).all()
                    lead_id_by_oid = {coid: str(xid) for (coid, xid) in rows if coid and xid}

                # NOTE: We deliberately do NOT call /api/v1/orders/byClientOid
                # here to "backfill" missing DB rows. That made one extra
                # signed request per stop order per refresh, multiplied by
                # the frontend's poll rate, and was causing API stack-up
                # that broke the Lead Trading connection check and other
                # parallel calls. With the new order_id encoding below
                # (stop:{id}:{clientOid}:{symbol}) the cancel handler has
                # everything it needs from the LIST response itself — no
                # DB lookup required.
                for s in items:
                    stop_dir = (s.get("stop") or "").lower()   # "up" | "down"
                    side     = (s.get("side") or "").lower()
                    is_reduce = bool(s.get("reduceOnly") or s.get("closeOrder"))
                    # Classify TP vs SL by stop direction + close side:
                    #   sell + up   → TP for LONG
                    #   sell + down → SL for LONG
                    #   buy  + down → TP for SHORT
                    #   buy  + up   → SL for SHORT
                    if is_reduce and stop_dir and side:
                        if (side == "sell" and stop_dir == "up") or (side == "buy" and stop_dir == "down"):
                            tp_or_sl = "tp"
                        else:
                            tp_or_sl = "sl"
                    else:
                        tp_or_sl = None
                    # Encode the cancel-relevant fields directly into
                    # order_id so the cancel handler never needs a DB
                    # lookup. Format: stop:{id}:{clientOid}:{symbol}
                    # Each piece base64url-safe (clientOid may have dashes
                    # but no slashes/colons). The cancel handler parses
                    # this back out and tries cancel-by-clientOid as the
                    # primary route (which is what KuCoin's docs say is
                    # the canonical Lead Trading cancel path).
                    client_oid = s.get("clientOid") or ""
                    lead_id    = lead_id_by_oid.get(client_oid) or s.get("id") or ""
                    sym        = s.get("symbol") or ""
                    stop_orders.append({
                        "order_id":   f"stop:{lead_id}:{client_oid}:{sym}",
                        "symbol":     s.get("symbol"),
                        "side":       side,
                        "order_type": s.get("type") or "market",
                        "size":       s.get("size") or 0,
                        "price":      s.get("price"),
                        "stop_price": s.get("stopPrice"),
                        "stop":       stop_dir,
                        "stop_price_type": s.get("stopPriceType"),
                        "leverage":   s.get("leverage"),
                        "margin_mode": s.get("marginMode"),
                        "mode":       "live",
                        "status":     "pending",
                        "kind":       "stop",            # marker for frontend
                        "tp_or_sl":   tp_or_sl,
                        "reduce_only": is_reduce,
                        "close_order": bool(s.get("closeOrder")),
                        "created_at": s.get("createdAt"),
                    })
        except Exception as e:
            log.warning("[%s] KuCoin stop-orders fetch failed: %s", user_id, e)

    query = select(FuturesOrder).where(FuturesOrder.user_id == user_id)
    if status:
        query = query.where(FuturesOrder.status == status)
    if symbol:
        query = query.where(FuturesOrder.symbol == symbol)
    if mode in ("paper", "live"):
        query = query.where(FuturesOrder.mode == mode)
    query = query.order_by(desc(FuturesOrder.created_at)).limit(100)

    db_orders = []
    for o in db.execute(query).scalars().all():
        is_stop = o.order_type in ("stop_tp", "stop_sl")
        # LIVE advanced orders (TP/SL stops) come from KuCoin's stopOrders
        # LIST above (`stop_orders`) — KuCoin is the source of truth — so
        # skip the DB mirror to avoid double-listing the same stop. PAPER
        # has no exchange, so the DB rows ARE the source: render them as
        # `kind="stop"` so the frontend shows them under Advanced Orders and
        # projects them onto the position's TP/SL column, exactly like live.
        if is_stop and o.mode != "paper":
            continue
        row = {
            "order_id": o.client_oid or str(o.id),
            "db_id": o.id,
            "symbol": o.symbol,
            "side": o.side,
            "order_type": o.order_type,
            "size": o.size,
            "price": o.price,
            "stop_price": o.stop_price,
            "leverage": o.leverage,
            "margin_mode": o.margin_mode,
            "mode": o.mode,
            "status": o.status,
            "filled_size": o.filled_size,
            "filled_price": o.filled_price,
            "tp_price": o.tp_price,
            "sl_price": o.sl_price,
            "created_at": str(o.created_at),
        }
        if is_stop:
            is_tp = o.order_type == "stop_tp"
            # Trigger side: long TP / short SL → up; long SL / short TP → down.
            trig_up = (o.side == "sell" and is_tp) or (o.side == "buy" and not is_tp)
            row.update({
                "kind":            "stop",          # frontend Advanced-Orders marker
                "tp_or_sl":        "tp" if is_tp else "sl",
                "stop":            "up" if trig_up else "down",
                "stop_price_type": "TP",            # Last Trade Price (parity w/ live)
                "reduce_only":     True,
                "close_order":     True,
            })
        db_orders.append(row)

    # Always return DB orders — the previous logic returned `engine_orders`
    # for pending status, gated by `eng._mode == mode`. That was buggy:
    #   - User places a live limit order → DB row gets mode='live' ✓
    #   - But `eng._mode` is 'paper' (default — no live bot running)
    #   - Live tab queries ?mode=live → eng._mode != 'live' → returned []
    #   - Paper tab queries ?mode=paper → eng._mode == 'paper' → returned
    #     engine_orders which CONTAINED the live order
    #   ⇒ live limit orders leaked into the Paper tab.
    # All orders are persisted to FuturesOrder on creation (see
    # place_futures_order), so the DB rows are the single source of truth.
    # The mode filter above (`FuturesOrder.mode == mode`) does the right
    # paper/live separation directly off the column.
    # Merge KuCoin stop orders ("Advanced Orders") at the top so users see
    # active TP/SL alongside their regular pending orders.
    return {"orders": stop_orders + db_orders}


@router.get("/orders/history")
def get_futures_order_history(
    symbol: str = None,
    limit: int = 50,
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get filled/cancelled order history, filtered by mode."""
    query = (
        select(FuturesOrder)
        .where(
            FuturesOrder.user_id == user_id,
            FuturesOrder.status.in_(["filled", "cancelled", "partially_filled"]),
        )
        .order_by(desc(FuturesOrder.created_at))
        .limit(limit)
    )
    if symbol:
        query = query.where(FuturesOrder.symbol == symbol)
    if mode in ("paper", "live"):
        query = query.where(FuturesOrder.mode == mode)

    orders = [
        {
            "order_id": o.client_oid or str(o.id),
            "symbol": o.symbol, "side": o.side, "order_type": o.order_type,
            "size": o.size, "price": o.price, "filled_size": o.filled_size,
            "filled_price": o.filled_price, "fee": o.fee, "status": o.status,
            "mode": o.mode,
            "created_at": str(o.created_at), "filled_at": str(o.filled_at) if o.filled_at else None,
        }
        for o in db.execute(query).scalars().all()
    ]
    return {"orders": orders}


# ── Leverage & Margin Mode ───────────────────────────────────────────────

@router.post("/leverage")
def set_futures_leverage(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set leverage for a symbol — updates engine state AND pushes to KuCoin."""
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol   = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    leverage = int(req.get("leverage", 10))
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_leverage(symbol, leverage)
    # Best-effort sync to KuCoin so order placement uses the same value.
    # Silent if creds aren't loaded yet — order path will sync on demand.
    if _ensure_live_credentials(eng, user_id, db)[0]:
        _sync_leverage_to_kucoin(eng, symbol, leverage, user_id)
    log_event(db, user_id, "futures.set_leverage", request, payload=result)
    return result


@router.post("/margin-mode")
def set_futures_margin_mode(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set margin mode (cross/isolated) for a symbol — updates engine AND KuCoin.

    Without the KuCoin sync, the next order would be rejected with
    "The order's margin mode does not match the selected one".
    """
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    mode   = req.get("mode", "cross")
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_margin(symbol, mode)

    # Push to KuCoin if creds available. If KuCoin refuses (e.g. open
    # position locks the mode), surface the error so the toggle visibly
    # reverts in the UI instead of silently going out of sync.
    if _ensure_live_credentials(eng, user_id, db)[0]:
        ok, err = _sync_margin_mode_to_kucoin(eng, symbol, mode, user_id)
        if not ok:
            result["warning"] = err
            result["synced_with_kucoin"] = False
        else:
            result["synced_with_kucoin"] = True

    log_event(db, user_id, "futures.set_margin_mode", request, payload=result)
    return result


@router.get("/leverage/{symbol}")
def get_futures_leverage(
    symbol: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get current leverage + margin mode for a symbol.

    For users with live KuCoin creds, returns KuCoin's REAL values (so the
    leverage selector in the UI reflects what's actually on the exchange,
    not just the engine's last local setting). Falls back to engine memory
    when creds aren't loaded.
    """
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol = normalize_futures_symbol(symbol)
    eng = futures_engine_registry.for_user(user_id)

    leverage    = eng.get_symbol_leverage(symbol)
    margin_mode = eng.get_symbol_margin(symbol)
    source      = "engine"

    # Live mode: prefer KuCoin's reality WHEN it's authoritative. This is
    # also what we'll pre-select in the leverage modal on page load.
    #
    # Critical bug fix: when the user has NO open position on the symbol
    # yet, KuCoin's /api/v1/position returns realLeverage=0 and
    # leverage=1 (the symbol-default that nobody has overridden via
    # changeLeverage). The old code took that "1" as truth and
    # overwrote the user's chosen leverage. Now we only override engine
    # leverage when KuCoin's value is meaningful (has a position OR
    # the configured leverage is non-default).
    if _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            # Single KuCoin call — extract everything we need from one
            # response (was previously TWO sequential round trips:
            # _fetch_kucoin_symbol_settings + a duplicate /position fetch
            # to decide if the value was meaningful). Saves ~300ms per
            # leverage GET, which the modal calls on every open.
            _raw = _kucoin_get_signed(
                "/api/v1/position",
                eng._api_key, eng._api_sec, eng._api_pass,
                params={"symbol": symbol},
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(_raw.get("code")) == "200000":
                _d = _raw.get("data") or {}
                _real = float(_d.get("realLeverage") or 0)
                _cfg  = _d.get("leverage")
                _cur_qty = abs(float(_d.get("currentQty") or 0))
                kucoin_has_position = (_real > 0) or (_cur_qty > 0)
                try:
                    kc_lev = int(round(float(_real) if _real > 0 else float(_cfg or 0)))
                except (TypeError, ValueError):
                    kc_lev = None
                kc_mode_raw = (_d.get("marginMode") or "").upper()
                if not kc_mode_raw:
                    cross_flag = _d.get("crossMode")
                    if cross_flag is True:
                        kc_mode_raw = "CROSS"
                    elif cross_flag is False:
                        kc_mode_raw = "ISOLATED"

                # Only override engine leverage when KuCoin's value is
                # meaningful — see comment above.
                if kc_lev and (kucoin_has_position or kc_lev > 1):
                    leverage = kc_lev
                    try:
                        eng.set_symbol_leverage(symbol, kc_lev)
                    except Exception:
                        pass
                    source = "kucoin"
                if kc_mode_raw in ("CROSS", "ISOLATED"):
                    margin_mode = kc_mode_raw.lower()
                    try:
                        eng.set_symbol_margin(symbol, margin_mode)
                    except Exception:
                        pass
        except Exception as e:
            log.debug("[%s] /leverage live fetch failed for %s: %s",
                      user_id, symbol, e)

    return {
        "symbol":      symbol,
        "leverage":    leverage,
        "margin_mode": margin_mode,
        "source":      source,    # "kucoin" if from exchange, "engine" otherwise
    }


# ── Account Overview ─────────────────────────────────────────────────────

@router.get("/account")
def futures_account(
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Futures account overview.
    mode=paper → always return paper engine balance.
    mode=live or None → try KuCoin, fallback to paper."""
    from backend.utils.encryption import decrypt, DecryptError

    eng = futures_engine_registry.for_user(user_id)

    # Paper mode: skip KuCoin, return paper engine balance directly
    if mode == "paper":
        return _paper_account(eng, user_id)

    # Live mode: try to fetch live data from KuCoin Futures account
    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if cfg:
        try:
            kk = decrypt(cfg.kucoin_key_enc or "", user_id)
            ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
            kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
            if kk and ks:
                from backend.services.native_trading_engine import _kucoin_get_signed
                from backend.services.futures_engine import KUCOIN_FUTURES_BASE

                # Fetch account overview from KuCoin Futures
                # Lead Trading shares the same futures account — this is the real balance
                data = _kucoin_get_signed(
                    "/api/v1/account-overview", kk, ks, kp,
                    params={"currency": "USDT"},
                    base_url=KUCOIN_FUTURES_BASE,
                )
                if str(data.get("code")) == "200000":
                    acct = data.get("data", {})
                    account_equity = float(acct.get("accountEquity", 0))
                    margin_balance = float(acct.get("marginBalance", 0))
                    available_balance = float(acct.get("availableBalance", 0))
                    unrealised_pnl = float(acct.get("unrealisedPNL", 0))
                    position_margin = float(acct.get("positionMargin", 0))
                    order_margin = float(acct.get("orderMargin", 0))
                    frozen_funds = float(acct.get("frozenFunds", 0))
                    risk_ratio = float(acct.get("riskRatio", 0))
                    max_withdraw = float(acct.get("maxWithdrawAmount", 0))

                    return {
                        "mode": "live",
                        "source": "kucoin_lead_trading",
                        "balance": account_equity,
                        "margin_balance": margin_balance,
                        "equity": account_equity,
                        "available_balance": available_balance,
                        "available_margin": float(acct.get("availableMargin", available_balance)),
                        "unrealized_pnl": unrealised_pnl,
                        "used_margin": position_margin,
                        "order_margin": order_margin,
                        "margin_mode": "Cross",
                        "frozen_funds": frozen_funds,
                        "risk_ratio": risk_ratio,
                        "max_withdraw": max_withdraw,
                        "currency": acct.get("currency", "USDT"),
                    }
                else:
                    log.warning("KuCoin account-overview error: %s %s",
                                data.get("code"), data.get("msg"))
        except (DecryptError, Exception) as exc:
            log.warning("Failed to fetch KuCoin lead trading account for %s: %s", user_id, exc)

    # Fallback: paper account from engine state (no KuCoin keys configured)
    return _paper_account(eng, user_id)


def _paper_account(eng, user_id: str = None):
    """Return paper account overview for the user's MAIN paper engine only.

    Earlier version summed balance across the main engine + every per-bot
    engine to fix "main balance never updates when a bot trades". But that
    caused a worse bug: each new bot's $1000 starter wallet was added to
    the global Asset Overview the moment the bot was created, so the
    user's "Lead Trading Connected • X USDT" number kept inflating with
    every bot they spun up.

    Now: Asset Overview = manual-trading wallet only (one source of truth).
    Per-bot wallets + P&L live on each bot card (where they belong). If a
    user wants to take the main wallet up after bot trades drained it,
    /paper/add-funds is the explicit knob for that.
    """
    if eng is None or getattr(eng, "_mode", "paper") != "paper":
        # No paper engine for this user — return an empty-balance shape so
        # the UI still renders fields without `?? 0` defaults everywhere.
        return {
            "mode": "paper", "source": "paper_engine_main", "engine_count": 0,
            "balance": 0.0, "margin_balance": 0.0, "equity": 0.0,
            "available_balance": 0.0, "available_margin": 0.0,
            "unrealized_pnl": 0.0, "used_margin": 0.0, "order_margin": 0,
            "margin_mode": "Isolated", "frozen_funds": 0, "risk_ratio": 0,
            "max_withdraw": 0.0, "position_count": 0, "currency": "USDT",
        }

    main_balance = float(eng.balance or 0)
    unrealized = 0.0
    used_margin = 0.0
    open_count = 0
    try:
        if eng.is_running:
            pos_list = eng.get_open_positions()
            unrealized = sum(p.get("unrealized_pnl", 0) for p in pos_list)
            used_margin = sum(p.get("stake", 0) for p in pos_list)
            open_count = len(pos_list)
    except Exception:
        pass

    return {
        "mode": "paper",
        "source": "paper_engine_main",
        "engine_count": 1,
        "balance": round(main_balance, 4),
        "margin_balance": round(main_balance, 4),
        "equity": round(main_balance + unrealized, 4),
        "available_balance": round(main_balance, 4),
        "available_margin": round(main_balance - used_margin, 4),
        "unrealized_pnl": round(unrealized, 4),
        "used_margin": round(used_margin, 4),
        "order_margin": 0,
        "margin_mode": "Isolated",
        "frozen_funds": 0,
        "risk_ratio": round(used_margin / max(main_balance, 0.01) * 100, 2) if used_margin > 0 else 0,
        "max_withdraw": round(main_balance, 4),
        "position_count": open_count,
        "currency": "USDT",
    }


# ── Paper Account Funding ────────────────────────────────────────────────

@router.post("/paper/add-funds")
def paper_add_funds(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Add (or reset) virtual USDT on the user's MAIN paper engine.

    Body: { "amount": float, "reset": bool (optional, default false) }
    """
    amount = req.get("amount")
    reset  = bool(req.get("reset", False))
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if not (0.01 <= amount <= 1_000_000):
        return {"error": "amount must be between 0.01 and 1,000,000 USDT"}

    eng = futures_engine_registry.for_user(user_id)
    if eng is None:
        return {"error": "no engine for user"}
    if getattr(eng, "_mode", "paper") == "live":
        return {"error": "Cannot add virtual funds to a LIVE engine. "
                         "Deposit on KuCoin directly for live trading."}

    with eng._lock:
        prev_balance = float(eng.balance or 0)
        new_balance = amount if reset else prev_balance + amount
        eng.balance = new_balance
        try:
            eng._log_action("paper_funds_added",
                f"{'RESET' if reset else 'ADDED'} {amount:.2f} USDT "
                f"(was {prev_balance:.2f}, now {new_balance:.2f})",
                amount=amount, reset=reset)
        except Exception:
            pass
    return {"ok": True, "mode": "paper",
            "action": "reset" if reset else "add",
            "prev_balance": round(prev_balance, 4),
            "new_balance":  round(new_balance, 4)}


@router.post("/paper/bot/{bot_id}/add-funds")
def paper_bot_add_funds(
    bot_id: int,
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Add (or reset) virtual USDT on a specific paper BOT engine."""
    amount = req.get("amount")
    reset  = bool(req.get("reset", False))
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if not (0.01 <= amount <= 1_000_000):
        return {"error": "amount must be between 0.01 and 1,000,000 USDT"}

    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if instance is None or instance.mode != "paper":
        return {"error": "Paper bot not found"}
    if not instance.engine_key:
        return {"error": "Bot has no engine_key"}
    eng = futures_engine_registry.for_bot(user_id, instance.engine_key)
    if eng is None:
        return {"error": "Engine not running — start the bot first"}

    with eng._lock:
        prev_balance = float(eng.balance or 0)
        new_balance  = amount if reset else prev_balance + amount
        eng.balance  = new_balance
        instance.wallet = new_balance
        db.commit()
        try:
            eng._log_action("paper_funds_added",
                f"{'RESET' if reset else 'ADDED'} {amount:.2f} USDT "
                f"(was {prev_balance:.2f}, now {new_balance:.2f})",
                amount=amount, reset=reset)
        except Exception:
            pass
    return {"ok": True, "bot_id": bot_id, "mode": "paper",
            "action": "reset" if reset else "add",
            "prev_balance": round(prev_balance, 4),
            "new_balance":  round(new_balance, 4)}


# ── Position Margin Management (Add / Reduce Margin) ─────────────────────

def _find_position_across_engines(
    user_id: str, pair: str, mode: str,
    *,
    direction: str | None = None,
    position_id: str | None = None,
):
    """Search main engine + all per-bot engines for an open position on
    `pair` whose per-position mode tag matches `mode`. Returns
    (engine, position, trade_key) or (None, None, None) if not found.

    Filters by POSITION._mode rather than ENGINE._mode: the main user
    engine always has _mode='paper' by default, even when it holds a
    manual LIVE position (the position is tagged at /manual-entry with
    the user's actual choice). Filtering by engine mode would skip the
    main engine for live mode and fail to find live manual positions.

    Optional refinements for hedge-mode safety:
      • direction="long"|"short" — when set, only matches that side.
      • position_id="eng:<trade_key>" or "db:<id>" — exact target row.
        If a "db:" id is passed there's no engine match by definition,
        so we return (None, None, None) and let the caller fall back
        to the DB-orphan path.

    Without these filters, a user with both LONG and SHORT positions
    on BTC/USDT (hedge mode) would get whichever side iterated first
    on every action — TP/SL editor, add-margin, etc. would silently
    operate on the wrong row.
    """
    # Decode position_id once. db:<id> ⇒ no engine match (orphan only).
    target_eng_key: str | None = None
    if isinstance(position_id, str) and position_id:
        if position_id.startswith("eng:"):
            target_eng_key = position_id[4:]
        elif position_id.startswith("db:"):
            return None, None, None

    candidates = []
    main_eng = futures_engine_registry.for_user(user_id)
    if main_eng is not None:
        candidates.append(main_eng)
    try:
        for _key, _bot_eng in futures_engine_registry.user_bot_engines(user_id):
            if _bot_eng is not None:
                candidates.append(_bot_eng)
    except Exception:
        pass
    for _e in candidates:
        for _k, p in _e.positions.items():
            if p.pair != pair:
                continue
            pos_mode = getattr(p, "_mode", getattr(_e, "_mode", "paper"))
            if pos_mode != mode:
                continue
            if direction is not None and p.direction != direction:
                continue
            if target_eng_key is not None and _k != target_eng_key:
                continue
            return _e, p, _k
    return None, None, None


def _recompute_liq_price(pos, prev_margin: float, new_margin: float):
    """Recompute the position's liquidation_price based on the new
    effective leverage after a margin add/reduce."""
    try:
        old_lev = float(getattr(pos, "leverage", 1) or 1)
        new_eff_lev = (prev_margin * old_lev) / max(new_margin, 0.01)
        if pos.direction == "long":
            pos.liquidation_price = pos.entry * (1.0 - 1.0 / max(new_eff_lev, 1.0))
        else:
            pos.liquidation_price = pos.entry * (1.0 + 1.0 / max(new_eff_lev, 1.0))
        return old_lev, new_eff_lev
    except Exception:
        return None, None


@router.post("/position/add-margin")
def position_add_margin(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Add margin to an open futures position (paper or live).

    Accepts optional `direction` and `position_id` so hedge-mode users
    can target a specific side (long or short on the same pair).
    """
    pair   = req.get("pair")
    direction_req = req.get("direction")
    if direction_req not in ("long", "short", None):
        direction_req = None
    position_id_req = req.get("position_id") if isinstance(req.get("position_id"), str) else None
    mode   = req.get("mode", "paper")
    try:
        amount = float(req.get("amount"))
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if not pair:
        return {"error": "pair is required"}
    if mode not in ("paper", "live"):
        return {"error": "mode must be 'paper' or 'live'"}
    if not (0.01 <= amount <= 1_000_000):
        return {"error": "amount must be between 0.01 and 1,000,000 USDT"}

    eng, pos, _ = _find_position_across_engines(
        user_id, pair, mode,
        direction=direction_req, position_id=position_id_req,
    )

    # ── DB-orphan fallback ─────────────────────────────────────────────
    # When the engine restarted (Railway redeploy etc.) the position may
    # exist ONLY in the Trade table — the engine forgot it. The /open
    # endpoint surfaces it via the DB-fallback path so the user sees the
    # row, but _find_position_across_engines returns nothing. Without
    # this branch the user clicks Margin → "No open position" error
    # despite seeing the row right there. Now we update the Trade row
    # directly (paper) or hit KuCoin (live, future support).
    if pos is None:
        target_db_id = None
        if position_id_req and position_id_req.startswith("db:"):
            try:
                target_db_id = int(position_id_req[3:])
            except ValueError:
                target_db_id = None

        orphan_q = select(Trade).where(
            Trade.user_id    == user_id,
            Trade.pair       == pair,
            Trade.market_type == "futures",
            Trade.mode       == mode,
            Trade.status     == "open",
        )
        if direction_req:
            orphan_q = orphan_q.where(Trade.side == direction_req)
        if target_db_id is not None:
            orphan_q = orphan_q.where(Trade.id == target_db_id)
        else:
            orphan_q = orphan_q.order_by(desc(Trade.entry_time)).limit(1)
        orphan = db.execute(orphan_q).scalar_one_or_none()
        if orphan is None:
            # ── KuCoin-direct fallback (LIVE only) ──────────────────────
            # The position can live ONLY on KuCoin — /open surfaces it as a
            # 'kucoin-…' row (engine state lost on a restart, opened in
            # KuCoin's own UI, or a filled limit order) with no engine or
            # Trade-row binding, so neither the engine search nor the orphan
            # query above finds it. Without this the user SEES the position
            # but add-margin always errors 'No open position'. Hit KuCoin's
            # deposit-margin endpoint directly using the live position.
            if mode == "live":
                kc_eng = futures_engine_registry.for_user(user_id)
                ok_creds, _cerr = _ensure_live_credentials(kc_eng, user_id, db)
                if ok_creds:
                    kc_pos = _fetch_kucoin_live_position(kc_eng, pair, direction_req)
                    if kc_pos is not None:
                        ok_dep, dep = _kucoin_deposit_margin(user_id, db, pair, amount)
                        if not ok_dep:
                            return {"error": dep}
                        prev_margin = float(kc_pos.get("margin") or 0)
                        new_eff_lev = (
                            (prev_margin * float(kc_pos.get("leverage") or 1))
                            / max(prev_margin + amount, 0.01)
                        )
                        return {
                            "ok": True, "mode": "live", "pair": pair,
                            "amount_added": amount,
                            "prev_margin": round(prev_margin, 4),
                            "new_margin": round(prev_margin + amount, 4),
                            "prev_leverage": round(float(kc_pos.get("leverage") or 0), 2),
                            "new_effective_leverage": round(new_eff_lev, 2),
                            "kucoin_response": dep,
                            "source": "kucoin_live",
                        }
            suffix = f" ({direction_req.upper()})" if direction_req else ""
            return {"error": f"No open position for {pair}{suffix} in {mode} mode"}

        # Paper orphan: shrink the user's main paper balance, grow the
        # row's amount, recompute the liq price from new effective lev.
        if mode == "paper":
            main_eng = futures_engine_registry.for_user(user_id)
            if main_eng is None:
                return {"error": "no engine for user"}
            with main_eng._lock:
                # See Model-B note in the engine path: adding margin must not
                # deduct from balance, or the wallet drains by `amount` (closes
                # never return margin). Only reallocate into the position.
                prev_margin = float(orphan.amount or 0)
                new_margin = prev_margin + amount
                orphan.amount = round(new_margin, 8)
                # Recompute liquidation_price from the new effective lev.
                lev_old = float(orphan.leverage or 1)
                new_eff_lev = (prev_margin * lev_old) / max(new_margin, 0.01)
                if (orphan.side or "long") == "long":
                    orphan.liquidation_price = round(
                        orphan.entry_price * (1.0 - 1.0 / max(new_eff_lev, 1.0)), 8
                    )
                else:
                    orphan.liquidation_price = round(
                        orphan.entry_price * (1.0 + 1.0 / max(new_eff_lev, 1.0)), 8
                    )
            db.commit()
            return {
                "ok": True, "mode": "paper", "pair": pair,
                "amount_added": amount,
                "prev_margin": round(prev_margin, 4),
                "new_margin": round(new_margin, 4),
                "prev_leverage": round(lev_old, 2),
                "new_effective_leverage": round(new_eff_lev, 2),
                "new_liquidation_price": orphan.liquidation_price,
                "source": "db_orphan",
            }
        # Live orphan: route through KuCoin's margin/deposit-margin.
        # Falls through to the existing live block below by synthesising
        # a minimal pos so the rest of the function works.
        class _Stub:
            pass
        pos = _Stub()
        pos.pair = pair
        pos.direction = orphan.side
        pos.entry = orphan.entry_price
        pos.size = float(orphan.amount or 0)
        pos.leverage = orphan.leverage or 1
        pos.liquidation_price = orphan.liquidation_price
        eng = futures_engine_registry.for_user(user_id)
        # Mark so the live success block updates the DB row, not pos.size
        # (pos here is a synthetic stub).
        pos._orphan_trade = orphan

    if mode == "paper":
        with eng._lock:
            # Realized-equity wallet model: margin is NOT drawn from balance
            # on open (closes credit only realized P&L, never return margin),
            # so adding margin must NOT touch balance either. It only
            # reallocates equity into this position — lowering effective
            # leverage and pushing the liq price away. Deducting here would
            # permanently drain the wallet by `amount` (same bug class as the
            # open-side margin drain fixed in 49f5b3e).
            prev_margin = float(getattr(pos, "size", 0) or 0)
            new_margin = prev_margin + amount
            pos.size = new_margin
            old_lev, new_eff_lev = _recompute_liq_price(pos, prev_margin, new_margin)
            try:
                eng._log_action("margin_added",
                    f"Added {amount:.2f} USDT to {pair} {pos.direction} "
                    f"(margin {prev_margin:.2f} → {new_margin:.2f})",
                    pair=pair, amount=amount,
                    prev_margin=prev_margin, new_margin=new_margin)
            except Exception:
                pass
        # Persist new margin + liq to the open Trade row so the change
        # survives a backend restart. Without this, adding margin in
        # memory is lost when the engine resets and /open re-renders
        # from the original DB amount.
        try:
            if getattr(pos, "db_id", None):
                db_row = db.execute(
                    select(Trade).where(Trade.id == pos.db_id)
                ).scalar_one_or_none()
                if db_row is not None:
                    db_row.amount = round(new_margin, 8)
                    if getattr(pos, "liquidation_price", None) is not None:
                        db_row.liquidation_price = round(pos.liquidation_price, 8)
                    db.commit()
        except Exception as persist_exc:
            log.warning("[%s] Failed to persist engine add-margin to DB: %s",
                        user_id, persist_exc)
        return {"ok": True, "mode": "paper", "pair": pair,
                "amount_added": amount,
                "prev_margin": round(prev_margin, 4),
                "new_margin": round(new_margin, 4),
                "prev_leverage": round(old_lev or 0, 2),
                "new_effective_leverage": round(new_eff_lev or 0, 2),
                "new_liquidation_price": round(pos.liquidation_price or 0, 4),
                "remaining_balance": round(eng.balance, 4)}

    # LIVE: deposit margin on KuCoin via the shared helper (canonical
    # /api/v1/position/margin/deposit-margin path).
    ok_dep, dep = _kucoin_deposit_margin(user_id, db, pair, amount)
    if not ok_dep:
        return {"error": dep}
    result_data = dep
    # Mirror locally so engine PNL math stays correct
    try:
        with eng._lock:
            prev_margin = float(getattr(pos, "size", 0) or 0)
            pos.size = prev_margin + amount
    except Exception:
        pass
    # If this was a DB-orphan stub (engine got reset, position lives
    # only in the Trade row), also persist to the DB so /open
    # reflects the new margin instead of the old.
    orphan_trade = getattr(pos, "_orphan_trade", None)
    if orphan_trade is not None:
        try:
            orphan_trade.amount = round(float(orphan_trade.amount or 0) + amount, 8)
            # Recompute liq price from the new effective leverage.
            lev_old = float(orphan_trade.leverage or 1)
            new_eff_lev = ((float(orphan_trade.amount or 0) - amount) * lev_old) / max(float(orphan_trade.amount or 0), 0.01)
            if (orphan_trade.side or "long") == "long":
                orphan_trade.liquidation_price = round(
                    orphan_trade.entry_price * (1.0 - 1.0 / max(new_eff_lev, 1.0)), 8
                )
            else:
                orphan_trade.liquidation_price = round(
                    orphan_trade.entry_price * (1.0 + 1.0 / max(new_eff_lev, 1.0)), 8
                )
            db.commit()
        except Exception as orphan_db_exc:
            log.warning("[%s] Failed to persist live orphan add-margin: %s",
                        user_id, orphan_db_exc)
    return {"ok": True, "mode": "live", "pair": pair,
            "amount_added": amount,
            "kucoin_response": result_data,
            "source": "db_orphan_live" if orphan_trade else "engine"}


@router.post("/position/reduce-margin")
def position_reduce_margin(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Withdraw margin from an open paper futures position.
    Live reduce-margin is more complex (requires risk-limit-level change
    on KuCoin) — for now use partial-close for live positions instead.
    """
    pair = req.get("pair")
    mode = req.get("mode", "paper")
    direction_req = req.get("direction") if req.get("direction") in ("long", "short") else None
    position_id_req = req.get("position_id") if isinstance(req.get("position_id"), str) else None
    try:
        amount = float(req.get("amount"))
    except (TypeError, ValueError):
        return {"error": "amount must be a number"}
    if not pair:
        return {"error": "pair is required"}
    if mode != "paper":
        return {"error": "Reduce margin on LIVE positions is not yet "
                         "supported. Use partial-close instead."}
    if not (0.01 <= amount <= 1_000_000):
        return {"error": "amount must be between 0.01 and 1,000,000 USDT"}

    eng, pos, _ = _find_position_across_engines(
        user_id, pair, "paper",
        direction=direction_req, position_id=position_id_req,
    )

    # ── DB-orphan fallback (same pattern as add-margin) ────────────────
    if pos is None:
        target_db_id = None
        if position_id_req and position_id_req.startswith("db:"):
            try:
                target_db_id = int(position_id_req[3:])
            except ValueError:
                target_db_id = None

        orphan_q = select(Trade).where(
            Trade.user_id    == user_id,
            Trade.pair       == pair,
            Trade.market_type == "futures",
            Trade.mode       == "paper",
            Trade.status     == "open",
        )
        if direction_req:
            orphan_q = orphan_q.where(Trade.side == direction_req)
        if target_db_id is not None:
            orphan_q = orphan_q.where(Trade.id == target_db_id)
        else:
            orphan_q = orphan_q.order_by(desc(Trade.entry_time)).limit(1)
        orphan = db.execute(orphan_q).scalar_one_or_none()
        if orphan is None:
            return {"error": f"No open paper position for {pair}"}

        main_eng = futures_engine_registry.for_user(user_id)
        prev_margin = float(orphan.amount or 0)
        min_margin = max(0.5, prev_margin * 0.10)
        if (prev_margin - amount) < min_margin:
            return {"error": f"Cannot reduce below {min_margin:.2f} USDT "
                             f"(10% safety floor)"}
        new_margin = prev_margin - amount
        orphan.amount = round(new_margin, 8)
        # Recompute liq price on the orphan row.
        lev_old = float(orphan.leverage or 1)
        new_eff_lev = (prev_margin * lev_old) / max(new_margin, 0.01)
        if (orphan.side or "long") == "long":
            orphan.liquidation_price = round(
                orphan.entry_price * (1.0 - 1.0 / max(new_eff_lev, 1.0)), 8
            )
        else:
            orphan.liquidation_price = round(
                orphan.entry_price * (1.0 + 1.0 / max(new_eff_lev, 1.0)), 8
            )
        # See Model-B note in the engine path: reducing margin must not
        # credit balance, or the wallet inflates by `amount` of phantom money.
        db.commit()
        return {
            "ok": True, "mode": "paper", "pair": pair,
            "amount_reduced": amount,
            "prev_margin": round(prev_margin, 4),
            "new_margin": round(new_margin, 4),
            "prev_leverage": round(lev_old, 2),
            "new_effective_leverage": round(new_eff_lev, 2),
            "new_liquidation_price": orphan.liquidation_price,
            "new_balance": round(float(main_eng.balance if main_eng else 0), 4),
            "source": "db_orphan",
        }

    with eng._lock:
        prev_margin = float(getattr(pos, "size", 0) or 0)
        min_margin = max(0.5, prev_margin * 0.10)
        if (prev_margin - amount) < min_margin:
            return {"error": f"Cannot reduce below {min_margin:.2f} USDT "
                             f"(10% safety floor)"}
        new_margin = prev_margin - amount
        pos.size = new_margin
        # Realized-equity wallet model: reducing margin must NOT credit
        # balance, or the wallet inflates by `amount` of phantom money
        # (mirror of the add-margin drain). Margin reduction only raises
        # effective leverage and moves the liq price closer.
        old_lev, new_eff_lev = _recompute_liq_price(pos, prev_margin, new_margin)
        try:
            eng._log_action("margin_reduced",
                f"Reduced {amount:.2f} USDT from {pair} {pos.direction} "
                f"(margin {prev_margin:.2f} → {new_margin:.2f})",
                pair=pair, amount=amount,
                prev_margin=prev_margin, new_margin=new_margin)
        except Exception:
            pass
    # Persist the reduction to DB so it survives engine restart.
    try:
        if getattr(pos, "db_id", None):
            db_row = db.execute(
                select(Trade).where(Trade.id == pos.db_id)
            ).scalar_one_or_none()
            if db_row is not None:
                db_row.amount = round(new_margin, 8)
                if getattr(pos, "liquidation_price", None) is not None:
                    db_row.liquidation_price = round(pos.liquidation_price, 8)
                db.commit()
    except Exception as persist_exc:
        log.warning("[%s] Failed to persist engine reduce-margin to DB: %s",
                    user_id, persist_exc)
    return {"ok": True, "mode": "paper", "pair": pair,
            "amount_reduced": amount,
            "prev_margin": round(prev_margin, 4),
            "new_margin": round(new_margin, 4),
            "prev_leverage": round(old_lev or 0, 2),
            "new_effective_leverage": round(new_eff_lev or 0, 2),
            "new_liquidation_price": round(pos.liquidation_price or 0, 4),
            "new_balance": round(eng.balance, 4)}


# ── Position TP/SL Management ────────────────────────────────────────────

@router.post("/position/tp-sl")
def set_position_tp_sl(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set or update TP/SL on an open futures position.

    Works for three sources of "open position":
      1. Engine in-memory positions (`eng.positions`) — local paper or
         live positions opened through our app.
      2. KuCoin-only positions — opened through our app but not tracked
         locally because the engine was restarted, OR opened externally
         on KuCoin. Falls back to /api/v1/position to fetch size/leverage
         from the exchange.
      3. DB open Trade rows — stores TP/SL for paper mode reconciliation.

    For live positions (whichever source), also places reduceOnly TP/SL
    stop orders on KuCoin Lead Trading so the TP/SL is visible there too.
    """
    from backend.services.native_trading_engine import (
        _kucoin_get_signed, _kucoin_post_signed,
    )
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    import time as _t

    pair     = req.get("pair", "BTC/USDT")
    tp_price = req.get("tp_price")
    sl_price = req.get("sl_price")
    # Hedge-safe: when present, only update THIS exact position. Without
    # these filters, a user with long + short on the same pair (hedge
    # mode) would have their TP/SL silently applied to whichever side
    # iterated first — the wrong one half the time.
    direction_req   = req.get("direction") if req.get("direction") in ("long", "short") else None
    position_id_req = req.get("position_id") if isinstance(req.get("position_id"), str) else None
    target_eng_key: str | None = None
    if position_id_req and position_id_req.startswith("eng:"):
        target_eng_key = position_id_req[4:]

    if tp_price is None and sl_price is None:
        return {"error": "Provide tp_price and/or sl_price."}

    eng = futures_engine_registry.for_user(user_id)
    kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

    # ── Step 1: find the position to attach TP/SL to ────────────────────
    matched_pos = None
    with eng._lock:
        for trade_key, pos in eng.positions.items():
            if pos.pair != pair:
                continue
            if direction_req is not None and pos.direction != direction_req:
                continue
            if target_eng_key is not None and trade_key != target_eng_key:
                continue
            if tp_price is not None:
                pos.tp = float(tp_price)
            if sl_price is not None:
                pos.sl = float(sl_price)
            matched_pos = pos
            break

    # KuCoin-side metadata (filled in below when needed). Either from the
    # local position object OR from a /api/v1/position fetch.
    kc_direction: str | None = None        # "long" | "short"
    kc_contracts: int = 0
    kc_leverage:  int = max(1, int(getattr(matched_pos, "leverage", 0) or eng._leverage or 1))
    kc_mode:      str = "paper"            # default when nothing is known
    kc_margin:    str = (eng.get_symbol_margin(kc_symbol) or "isolated").upper()

    if matched_pos:
        kc_direction = matched_pos.direction
        # Per-symbol contract sizing (was BTC-only ×1000 — wrong for every
        # non-BTC coin). closeOrder=True means the stop closes the whole
        # position regardless of size, but we keep the size accurate and
        # consistent with the entry/close paths anyway.
        _lot          = _futures_lot_size(kc_symbol)
        _base_qty     = (matched_pos.size * kc_leverage) / matched_pos.entry
        kc_contracts  = max(1, int(_base_qty / max(_lot, 1e-9)))
        kc_mode       = getattr(matched_pos, "_mode", None) or eng._mode or "paper"

    # ── Step 2a: PAPER DB-orphan fallback ────────────────────────────────
    # If no engine match AND the row is a paper position visible only in
    # the DB (engine got reset), RESURRECT it into the main user engine
    # first, then set TP/SL on the live engine pos. After resurrection
    # the manual-position watchdog auto-ticks TP/SL/liq every 5s — so
    # the stops the user just set actually fire. Without resurrection,
    # writing TP/SL to a DB-only row left no thread checking them.
    if not matched_pos:
        orphan_q = select(Trade).where(
            Trade.user_id    == user_id,
            Trade.pair       == pair,
            Trade.market_type == "futures",
            Trade.mode       == "paper",
            Trade.status     == "open",
        )
        if direction_req:
            orphan_q = orphan_q.where(Trade.side == direction_req)
        if position_id_req and position_id_req.startswith("db:"):
            try:
                orphan_q = orphan_q.where(Trade.id == int(position_id_req[3:]))
            except ValueError:
                pass
        else:
            orphan_q = orphan_q.order_by(desc(Trade.entry_time)).limit(1)
        orphan = db.execute(orphan_q).scalar_one_or_none()
        if orphan is not None:
            from backend.services.futures_engine import FuturesPosition, _calc_liquidation_price
            from datetime import timezone as _tz
            # Resurrect into the main user engine so the watchdog ticks it.
            with eng._lock:
                new_pos = FuturesPosition(
                    pair      = orphan.pair,
                    direction = orphan.side or "long",
                    entry     = float(orphan.entry_price or 0),
                    sl        = float(sl_price if sl_price is not None
                                       else (orphan.stoploss_price or 0)),
                    tp        = float(tp_price if tp_price is not None
                                       else (orphan.entry_price or 0)),
                    size      = float(orphan.amount or 0),
                    leverage  = int(orphan.leverage or 1),
                    opened_at = orphan.entry_time or datetime.now(_tz.utc),
                )
                new_pos._mode = "paper"
                new_pos.db_id = orphan.id
                # Use the same trade_key format as /manual-entry so /open
                # can de-dup if the engine somehow already had this row.
                trade_key = f"{orphan.pair}-{orphan.side}-resurrected-{int((orphan.entry_time or datetime.now(_tz.utc)).timestamp())}"
                eng.positions[trade_key] = new_pos
            # Persist the new SL on the DB row so the next reconcile
            # cycle agrees with the engine.
            if sl_price is not None:
                orphan.stoploss_price = round(float(sl_price), 8)
                db.commit()
            return {
                "ok": True,
                "pair": pair,
                "mode": "paper",
                "tp_price": float(tp_price) if tp_price is not None else None,
                "sl_price": float(sl_price) if sl_price is not None else None,
                "source": "db_orphan_resurrected",
                "note": (
                    "Position was DB-only (engine got reset). Resurrected "
                    "into the main engine and set TP/SL — the manual-"
                    "position watchdog now ticks it every 5s and will "
                    "auto-close on TP/SL/liq hit."
                ),
            }

    # ── Step 2b: if no engine position AND no paper orphan, try KuCoin ──
    if not matched_pos:
        if not _ensure_live_credentials(eng, user_id, db)[0]:
            return {"error": f"No open position for {pair}. Connect a Lead Trading API key in Setup to enable TP/SL on KuCoin-only positions."}
        # Use /api/v1/positions (LIST) + filter, NOT /api/v1/position?symbol=X
        # (SINGLE). The single-position endpoint returns qty=0 for Lead
        # Trading positions even when they're real and visible in the LIST
        # endpoint (which is what powers the reconcile that surfaces them
        # in the UI). Same bug we fixed for force-close.
        try:
            pos_resp = _kucoin_get_signed(
                "/api/v1/positions",
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
        except Exception as e:
            log.exception("[%s] Failed to fetch KuCoin position for TP/SL", user_id)
            return {"error": f"Could not read your KuCoin position: {e}"}
        if str(pos_resp.get("code")) != "200000":
            return {"error": f"KuCoin rejected position lookup: {pos_resp.get('msg', pos_resp)}"}
        pdata: dict = {}
        qty = 0
        kc_direction = "long"
        for _p in (pos_resp.get("data") or []):
            if (_p.get("symbol") or "").upper() != kc_symbol.upper():
                continue
            _q = int(_p.get("currentQty", 0) or 0)
            if _q == 0:
                continue
            # Hedge-mode aware direction: prefer positionSide (hedge reports
            # qty as a positive magnitude); fall back to qty sign for one-way.
            _pside = str(_p.get("positionSide", "") or "").upper()
            if _pside == "LONG":
                _row_dir = "long"
            elif _pside == "SHORT":
                _row_dir = "short"
            else:
                _row_dir = "long" if _q > 0 else "short"
            # In hedge mode both sides can be open on one symbol — respect the
            # requested direction so TP/SL lands on the intended leg instead of
            # whichever the LIST returns first.
            if direction_req in ("long", "short") and _row_dir != direction_req:
                continue
            pdata = _p
            qty = _q
            kc_direction = _row_dir
            break
        if qty == 0:
            return {"error": f"No open position for {pair} on KuCoin Lead Trading either. Open one before setting TP/SL."}
        kc_contracts = abs(qty)
        # Prefer the real per-symbol leverage KuCoin returns; falls back to
        # configured if KuCoin's value is missing or zero.
        try:
            real_lev = float(pdata.get("realLeverage") or 0)
            cfg_lev  = float(pdata.get("leverage") or 0)
            chosen   = real_lev if real_lev > 0 else cfg_lev
            if chosen > 0:
                kc_leverage = max(1, int(round(chosen)))
        except (TypeError, ValueError):
            pass
        kc_margin = (pdata.get("marginMode") or kc_margin).upper()
        # An open position on KuCoin means this is a LIVE flow.
        kc_mode = "live"

    # ── Step 3: place TP/SL stop orders on KuCoin for live positions ────
    kc_results: dict[str, object] = {}
    if kc_mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            kc_results["warning"] = err
        elif kc_direction is None:
            kc_results["warning"] = "Could not determine position direction."
        else:
            position_side = "LONG" if kc_direction == "long" else "SHORT"
            close_side    = "sell" if kc_direction == "long" else "buy"
            lev           = min(LEAD_MAX_LEVERAGE, kc_leverage)
            margin_mode   = kc_margin or "ISOLATED"

            # ────────────────────────────────────────────────────────────
            # Why this body shape:
            #
            # KuCoin Futures stop orders are placed via the *regular* orders
            # endpoint (NOT a separate /st-orders endpoint) with three
            # required fields that make it a stop order instead of a market
            # order:
            #     stop:           "up" | "down"   — trigger direction
            #     stopPrice:      "<price>"      — trigger price
            #     stopPriceType:  "TP" | "MP" | "IP" — Last/Mark/Index price source
            #
            # The Lead Trading wrapper is /api/v1/copy-trade/futures/orders
            # and accepts the same fields.
            #
            # Earlier versions of this code used `triggerStopUpPrice` /
            # `triggerStopDownPrice` against an `/api/v1/copy-trade/futures/
            # st-orders` endpoint. Those field names belong to KuCoin's
            # position-attached-TP/SL feature on the order-CREATION body
            # (attach TP/SL to a NEW order as it's placed), not to stand-
            # alone stop orders. When KuCoin's API saw our body without a
            # recognized stop trigger, it interpreted the order as an
            # IMMEDIATE market + reduceOnly + closeOrder=true (if set)
            # request — which fired the instant it was POSTed and closed
            # the entire position. That's exactly the bug the user kept
            # reporting.
            #
            # The fix: use the canonical `stop` + `stopPrice` fields on the
            # regular Lead Trading orders endpoint with reduceOnly=true and
            # closeOrder=true. KuCoin then queues a real stop order that
            # waits for the trigger price.
            # ────────────────────────────────────────────────────────────
            def _stop_order(label: str, price: float, is_tp: bool) -> dict:
                # Trigger side depends on direction × order kind:
                # Long TP / Short SL  → trigger up
                # Long SL / Short TP  → trigger down
                trig_up = (kc_direction == "long" and is_tp) or (kc_direction == "short" and not is_tp)
                body: dict = {
                    "clientOid":     f"atf-{label}-{int(_t.time() * 1000)}",
                    "symbol":         kc_symbol,
                    "marginMode":     margin_mode,
                    "leverage":       lev,
                    "positionSide":   position_side,
                    "side":           close_side,
                    "type":           "market",
                    "size":           kc_contracts,
                    # Stop-order trigger fields (canonical KuCoin Futures):
                    "stop":           "up" if trig_up else "down",
                    "stopPrice":      str(float(price)),
                    "stopPriceType":  "TP",   # trigger from Last Trade Price
                    "reduceOnly":     True,
                    "closeOrder":     True,   # closes entire position on trigger
                }
                return body

            def _place_stop(label: str, price: float, is_tp: bool):
                body = _stop_order(label, price, is_tp)
                log.info("[%s] Placing %s stop order: %s", user_id, label.upper(), body)
                try:
                    resp = _kucoin_post_signed(
                        "/api/v1/copy-trade/futures/orders",
                        body,
                        eng._api_key, eng._api_sec, eng._api_pass,
                        base_url=KUCOIN_FUTURES_BASE,
                    )
                except Exception as e:
                    log.exception("[%s] Lead Trading %s failed", user_id, label.upper())
                    kc_results[label] = {"error": str(e)}
                    return
                code = str(resp.get("code", ""))
                kc_results[label] = {"code": code, "msg": resp.get("msg"), "data": resp.get("data")}
                if code == "200000":
                    log.info("[%s] Lead Trading %s stop order placed for %s @ %s",
                             user_id, label.upper(), pair, price)
                    # Persist the Lead Trading orderId so the cancel button
                    # has the correct namespace ID. /api/v1/stopOrders LIST
                    # returns regular-futures IDs that don't work with the
                    # /copy-trade/futures/orders/{id} cancel route.
                    try:
                        kc_order_id = (resp.get("data") or {}).get("orderId")
                        if kc_order_id:
                            db.add(FuturesOrder(
                                user_id=user_id,
                                client_oid=body["clientOid"],
                                exchange_order_id=str(kc_order_id),
                                symbol=kc_symbol,
                                side=close_side,
                                order_type=f"stop_{label}",   # "stop_tp" / "stop_sl"
                                size=kc_contracts,
                                stop_price=float(price),
                                leverage=lev,
                                # DB CheckConstraint requires lowercase
                                # ('cross','isolated'); kc_margin is .upper()'d
                                # for the KuCoin API body, so normalise here or
                                # the insert silently rolls back — which would
                                # leave the /open overlay with no stop row and
                                # break the TP/SL-on-row display.
                                margin_mode=(margin_mode or "isolated").lower(),
                                mode="live",
                                status="pending",
                                tp_price=float(price) if is_tp else None,
                                sl_price=float(price) if not is_tp else None,
                                created_at=datetime.utcnow(),
                            ))
                            db.commit()
                    except Exception as persist_err:
                        log.warning("[%s] Failed to persist %s stop order id: %s",
                                    user_id, label.upper(), persist_err)
                        db.rollback()
                else:
                    log.warning("[%s] Lead Trading %s rejected: %s",
                                user_id, label.upper(), resp)

            # ── Cancel EXISTING stop orders for the legs we're replacing ──
            # Bug 6 ("automated sl is also not changing"): updating TP/SL used
            # to place a SECOND stop order on KuCoin while the OLD one stayed
            # live. KuCoin then held two stops for the same leg — the stale
            # one could still trigger first, so the user's change appeared to
            # have no effect. We now cancel the prior pending stop for each
            # leg (by its recorded exchange orderId) BEFORE placing the new
            # one. orderId is a QUERY param (same correction as the cancel
            # route). Best-effort: a failed cancel is logged but doesn't block
            # the new stop from being placed.
            from backend.services.kucoin_futures_client import _sign_request as _sign_cancel
            from backend.services._kucoin_proxy import urlopen as _cancel_urlopen
            import urllib.request as _ureq, urllib.error as _uerr, json as _cjson
            from urllib.parse import urlencode as _cancel_urlencode

            def _cancel_existing_stop(label: str) -> bool:
                """Cancel the recorded pending stop(s) for this leg BEFORE
                placing the replacement, so an edit UPDATES the TP/SL instead
                of stacking a second Advanced Order.

                Returns True when it's safe to place the replacement (every
                prior stop was cancelled OR is already gone from KuCoin), and
                False when a prior stop is confirmed STILL ACTIVE on KuCoin —
                in which case the caller must NOT place a duplicate.

                The previous version marked the DB row 'cancelled'
                unconditionally and tried only ONE cancel endpoint. When that
                endpoint failed, the stop survived on KuCoin while the DB
                thought it was gone — so the next edit couldn't find it to
                cancel and every edit stacked another live stop (the exact
                "different Advanced orders are creating" bug). We now try BOTH
                documented Lead Trading cancel routes (by orderId AND by
                clientOid, query-param form) and only flip the DB row once
                KuCoin confirms the cancel (or reports the order already gone).
                """
                order_type = f"stop_{label}"   # "stop_tp" | "stop_sl"
                try:
                    # Filter by close_side too: in hedge mode a pair holds a
                    # long AND a short, each with its own reduceOnly stops
                    # (recorded with side=close_side — "sell" for a long,
                    # "buy" for a short). Without the side filter, editing one
                    # leg's TP/SL would cancel the OTHER leg's stop as well.
                    rows = db.execute(
                        select(FuturesOrder).where(
                            FuturesOrder.user_id    == user_id,
                            FuturesOrder.symbol     == kc_symbol,
                            FuturesOrder.order_type == order_type,
                            FuturesOrder.side       == close_side,
                            FuturesOrder.status     == "pending",
                        )
                    ).scalars().all()
                except Exception as q_err:
                    log.warning("[%s] Could not query stale %s stops: %s",
                                user_id, order_type, q_err)
                    return True   # nothing we can see to cancel — allow placing

                def _cancel_one(ex_id, c_oid, sym) -> tuple[bool, bool]:
                    """Cancel ONE live stop. Returns (cancelled_ok, already_gone)."""
                    routes: list[tuple[str, dict]] = []
                    if ex_id:
                        routes.append(("/api/v1/copy-trade/futures/orders",
                                       {"orderId": ex_id}))
                    if c_oid and sym:
                        routes.append(("/api/v1/copy-trade/futures/orders/client-order",
                                       {"symbol": sym, "clientOid": c_oid}))
                    for endpoint, q in routes:
                        ep = endpoint + "?" + _cancel_urlencode(q)
                        try:
                            ts = str(int(_t.time() * 1000))
                            headers = _sign_cancel(
                                eng._api_sec, eng._api_pass, eng._api_key,
                                ts, "DELETE", ep,
                            )
                            req_obj = _ureq.Request(
                                f"{KUCOIN_FUTURES_BASE}{ep}", headers=headers, method="DELETE")
                            with _cancel_urlopen(req_obj, timeout=8) as r:
                                c_resp = _cjson.loads(r.read().decode())
                            if str(c_resp.get("code")) == "200000":
                                return True, False
                            msg = str(c_resp.get("msg") or "").lower()
                            if any(s in msg for s in ("not exist", "does not exist",
                                                      "not found", "already")):
                                return False, True   # already off the book
                        except _uerr.HTTPError as he:
                            if he.code == 404:
                                return False, True
                            try:
                                body = _cjson.loads(he.read().decode() or "{}")
                                msg = str(body.get("msg") or "").lower()
                                if any(s in msg for s in ("not exist", "does not exist",
                                                          "not found", "already")):
                                    return False, True
                            except Exception:
                                pass
                        except Exception as ce:
                            log.warning("[%s] cancel %s for stop %s errored: %s",
                                        user_id, endpoint, ex_id, ce)
                    return False, False

                safe = True
                for row in rows:
                    if row.exchange_order_id:
                        cancelled, gone = _cancel_one(
                            row.exchange_order_id, row.client_oid, row.symbol or kc_symbol)
                        if cancelled or gone:
                            row.status = "cancelled"
                            row.cancelled_at = datetime.utcnow()
                            log.info("[%s] Replaced stale %s stop %s (cancelled=%s gone=%s)",
                                     user_id, order_type, row.exchange_order_id,
                                     cancelled, gone)
                        else:
                            safe = False
                            log.warning("[%s] Stale %s stop %s still ACTIVE on KuCoin — "
                                        "not stacking a duplicate", user_id, order_type,
                                        row.exchange_order_id)
                    else:
                        # No exchange id recorded (shouldn't happen for live) —
                        # mark cancelled so it stops shadowing future edits.
                        row.status = "cancelled"
                        row.cancelled_at = datetime.utcnow()
                if rows:
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()
                return safe

            if tp_price is not None:
                if _cancel_existing_stop("tp"):
                    _place_stop("tp", float(tp_price), True)
                else:
                    kc_results["tp"] = {"error": (
                        "The existing take-profit stop is still active on KuCoin "
                        "and could not be cancelled, so a new one was not placed "
                        "to avoid duplicates. Please try again in a moment.")}
            if sl_price is not None:
                if _cancel_existing_stop("sl"):
                    _place_stop("sl", float(sl_price), False)
                else:
                    kc_results["sl"] = {"error": (
                        "The existing stop-loss is still active on KuCoin and "
                        "could not be cancelled, so a new one was not placed to "
                        "avoid duplicates. Please try again in a moment.")}

            # If BOTH sides failed, surface a clear error to the frontend.
            # If at least one succeeded, the partial success is reported in
            # `kc_results` so the modal can show which leg landed.
            tp_ok = kc_results.get("tp", {}).get("code") == "200000" if tp_price is not None else True
            sl_ok = kc_results.get("sl", {}).get("code") == "200000" if sl_price is not None else True
            if not tp_ok and not sl_ok:
                tp_msg = (kc_results.get("tp") or {}).get("msg") or (kc_results.get("tp") or {}).get("error")
                sl_msg = (kc_results.get("sl") or {}).get("msg") or (kc_results.get("sl") or {}).get("error")
                return {"error": f"KuCoin rejected TP/SL — TP: {tp_msg or 'n/a'} | SL: {sl_msg or 'n/a'}",
                        "kucoin": kc_results}

    # ── Step 3 (paper): record DB-only TP/SL stop orders ────────────────
    # Paper has no exchange, but we still create "stop_tp"/"stop_sl"
    # FuturesOrder rows so the Advanced Orders tab AND the position's TP/SL
    # column behave EXACTLY like live — letting the user dry-run the whole
    # basic/advanced-order flow risk-free. Editing cancels the prior leg
    # stop first, so an update REPLACES the TP/SL instead of stacking a
    # duplicate (mirrors the live fix above). The manual-position watchdog
    # already auto-closes the paper position when pos.tp/pos.sl is hit; the
    # /open paper-cleanup then retires these rows so the tab stays honest.
    elif kc_mode == "paper" and kc_direction is not None:
        paper_close_side = "sell" if kc_direction == "long" else "buy"
        paper_lev        = max(1, int(kc_leverage or 1))
        paper_margin     = (kc_margin or "ISOLATED")

        def _cancel_paper_stop(label: str) -> None:
            order_type = f"stop_{label}"
            try:
                rows = db.execute(
                    select(FuturesOrder).where(
                        FuturesOrder.user_id    == user_id,
                        FuturesOrder.symbol     == kc_symbol,
                        FuturesOrder.order_type == order_type,
                        FuturesOrder.side       == paper_close_side,
                        FuturesOrder.mode       == "paper",
                        FuturesOrder.status     == "pending",
                    )
                ).scalars().all()
            except Exception:
                return
            for row in rows:
                row.status = "cancelled"
                row.cancelled_at = datetime.utcnow()
            if rows:
                try:
                    db.commit()
                except Exception:
                    db.rollback()

        def _record_paper_stop(label: str, price: float, is_tp: bool) -> None:
            try:
                db.add(FuturesOrder(
                    user_id=user_id,
                    client_oid=f"paper-{label}-{int(_t.time() * 1000)}",
                    exchange_order_id=None,
                    symbol=kc_symbol,
                    side=paper_close_side,
                    order_type=f"stop_{label}",   # "stop_tp" / "stop_sl"
                    size=kc_contracts,
                    stop_price=float(price),
                    leverage=paper_lev,
                    # Lowercase to satisfy the margin_mode CheckConstraint
                    # ('cross','isolated'); paper_margin is uppercase.
                    margin_mode=(paper_margin or "isolated").lower(),
                    mode="paper",
                    status="pending",
                    tp_price=float(price) if is_tp else None,
                    sl_price=float(price) if not is_tp else None,
                    created_at=datetime.utcnow(),
                ))
                db.commit()
            except Exception as e:
                log.warning("[%s] Failed to record paper %s stop: %s", user_id, label, e)
                db.rollback()

        if tp_price is not None:
            _cancel_paper_stop("tp")
            _record_paper_stop("tp", float(tp_price), True)
            kc_results["tp"] = {"code": "paper", "stop_price": float(tp_price)}
        if sl_price is not None:
            _cancel_paper_stop("sl")
            _record_paper_stop("sl", float(sl_price), False)
            kc_results["sl"] = {"code": "paper", "stop_price": float(sl_price)}

    # ── Step 4: persist on the DB Trade row (paper reconciliation + UI) ──
    # Direction-filtered so a hedge user's long/short legs don't clobber
    # each other's stop-loss on the most-recent row for the pair.
    trade_q = select(Trade).where(
        Trade.user_id == user_id, Trade.pair == pair,
        Trade.market_type == "futures", Trade.status == "open",
    )
    if direction_req:
        trade_q = trade_q.where(Trade.side == direction_req)
    trade = db.execute(
        trade_q.order_by(desc(Trade.entry_time)).limit(1)
    ).scalar_one_or_none()
    if trade:
        if sl_price is not None:
            trade.stoploss_price = float(sl_price)
        db.commit()

    log_event(db, user_id, "futures.set_tp_sl", request,
              payload={"pair": pair, "tp": tp_price, "sl": sl_price,
                       "source": "engine" if matched_pos else "kucoin"})
    return {
        "updated": True,
        "pair": pair,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "source": "engine" if matched_pos else "kucoin",
        "kucoin": kc_results,
    }


# ── Bot Management ───────────────────────────────────────────────────────

@router.get("/lead-trading-status")
def lead_trading_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Check if the user has a KuCoin Futures Lead Trading API key configured."""
    from backend.utils.encryption import decrypt, DecryptError

    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        return {"connected": False, "reason": "No config found"}
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return {"connected": False, "reason": "Could not decrypt credentials"}
    if not kk or not ks:
        return {"connected": False, "reason": "No API key configured"}

    # Try to hit KuCoin Futures account overview to verify credentials + permissions
    try:
        from backend.services.native_trading_engine import _kucoin_get_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"},
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) == "200000":
            acct = data.get("data", {})
            return {
                "connected": True,
                "account_type": "futures_lead_trading",
                "balance": float(acct.get("availableBalance", 0)),
                "equity": float(acct.get("accountEquity", 0)),
                "unrealized_pnl": float(acct.get("unrealisedPNL", 0)),
            }
        code = str(data.get("code", ""))
        return {"connected": False, "reason": f"KuCoin error {code}: {data.get('msg', 'unknown')}"}
    except Exception as e:
        return {"connected": False, "reason": str(e)}


# Built-in signal criteria extracted from strategy signal function docstrings
_BUILTIN_SIGNAL_CRITERIA: dict[str, list[dict]] = {
    "SimpleTargetStrategy": [
        {"name": "LONG", "conditions": ["RSI < 55 AND price near/below EMA20", "OR RSI < 38 (strong oversold)"]},
        {"name": "SHORT", "conditions": ["RSI > 65 AND price above EMA20", "OR RSI > 72 (strong overbought)"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%", "R:R = 2:1"]},
    ],
    "BidirectionalStrategy": [
        {"name": "LONG", "conditions": ["EMA9 > EMA21 (uptrend, 2+ bars)", "RSI < 60"]},
        {"name": "SHORT", "conditions": ["EMA9 < EMA21 (downtrend, 2+ bars)", "RSI > 40"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%", "R:R = 2:1"]},
    ],
    "SMCStrategy": [
        {"name": "HTF Bias", "conditions": ["EMA50 direction — bullish if close > EMA50"]},
        {"name": "BOS", "conditions": ["Price breaks 20-bar swing high (LONG) or low (SHORT)"]},
        {"name": "FVG", "conditions": ["3-candle Fair Value Gap within last 30 bars"]},
        {"name": "OB Zone", "conditions": ["Last opposing candle before the move (Order Block)"]},
        {"name": "LONG", "conditions": ["Bullish BOS + Bullish FVG + price in OB zone + close > EMA50"]},
        {"name": "SHORT", "conditions": ["Bearish BOS + Bearish FVG + price in OB zone + close < EMA50"]},
        {"name": "Risk", "conditions": ["SL: below swing low (LONG) / above swing high (SHORT)", "TP: 2R from entry"]},
    ],
    "SMCStrategyTV": [
        {"name": "HTF Bias", "conditions": ["EMA50 direction — bullish if close > EMA50"]},
        {"name": "BOS", "conditions": ["Price breaks 20-bar swing high (LONG) or low (SHORT)"]},
        {"name": "FVG", "conditions": ["3-candle Fair Value Gap within last 30 bars"]},
        {"name": "OB Zone", "conditions": ["Last opposing candle before the move (Order Block)"]},
        {"name": "LONG", "conditions": ["Bullish BOS + Bullish FVG + price in OB zone + close > EMA50"]},
        {"name": "SHORT", "conditions": ["Bearish BOS + Bearish FVG + price in OB zone + close < EMA50"]},
        {"name": "Risk", "conditions": ["SL: below swing low (LONG) / above swing high (SHORT)", "TP: 2R from entry"]},
    ],
    "MissCandleLongStrategy": [
        {"name": "LONG", "conditions": ["Close crosses above upper Bollinger Band", "RSI momentum confirmation"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "MissCandleShortStrategy": [
        {"name": "SHORT", "conditions": ["Close crosses below lower Bollinger Band", "RSI momentum confirmation"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "MacdCrossoverStrategy": [
        {"name": "LONG", "conditions": ["MACD line crosses above Signal line"]},
        {"name": "SHORT", "conditions": ["MACD line crosses below Signal line"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%"]},
    ],
    "RsiBollingerStrategy": [
        {"name": "LONG", "conditions": ["RSI < 30 (oversold)", "Price touches lower Bollinger Band"]},
        {"name": "SHORT", "conditions": ["RSI > 70 (overbought)", "Price touches upper Bollinger Band"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "EmaScalpingStrategy": [
        {"name": "LONG", "conditions": ["EMA9 crosses above EMA21", "Volume confirmation"]},
        {"name": "SHORT", "conditions": ["EMA9 crosses below EMA21", "Volume confirmation"]},
        {"name": "Risk", "conditions": ["SL: 0.5%", "TP: 1.0%"]},
    ],
}


def _extract_signal_criteria(strategy_name: str, strategy_id: int | None, db, user_id: str) -> list[dict]:
    """Extract signal firing criteria from strategy. Uses built-in map or strategy description."""
    # Check built-in strategies first
    for key, criteria in _BUILTIN_SIGNAL_CRITERIA.items():
        if key.lower() in (strategy_name or "").lower() or (strategy_name or "").lower() in key.lower():
            return criteria

    # For user strategies, extract from description
    if strategy_id:
        from backend.models.strategy import Strategy
        from sqlalchemy import or_
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),
            )
        ).scalar_one_or_none()
        if strat and strat.description:
            return [{"name": "Strategy", "conditions": [strat.description[:200]]}]

    return []


@router.get("/bots")
def list_futures_bots(
    mode: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """List futures bot instances for this user, optionally filtered by mode (paper/live)."""
    query = (
        select(StrategyInstance)
        .where(StrategyInstance.user_id == user_id, StrategyInstance.market_type == "futures")
    )
    if mode in ("paper", "live"):
        query = query.where(StrategyInstance.mode == mode)
    query = query.order_by(desc(StrategyInstance.created_at))
    instances = db.execute(query).scalars().all()

    # Check actual engine status for running bots
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}

    # Auto-resume: restart engines for bots marked running in DB but with no
    # live thread. Throttled per-user — the dedicated 60s _bot_watchdog in
    # main.py already revives dead engines on its own cadence; running the
    # resume scan on EVERY /bots poll (UI refreshes every 8s) was making
    # this endpoint slow when several bots had decryption / start_futures
    # latency. We still run it on the FIRST poll after each interval so a
    # user opening the panel immediately after a backend restart gets an
    # instant revive.
    import time as _time_mod
    global _last_bots_autoresume_ts
    try:
        _last_bots_autoresume_ts
    except NameError:
        _last_bots_autoresume_ts = {}
    AUTORESUME_THROTTLE_SECS = 30.0
    _now_resume = _time_mod.time()
    _last_for_user = _last_bots_autoresume_ts.get(user_id, 0.0)
    _should_autoresume = (_now_resume - _last_for_user) >= AUTORESUME_THROTTLE_SECS

    _kk = _ks = _kp = ""
    _creds_loaded = False
    for i in instances:
        if not i.is_running or not i.engine_key:
            continue
        eng = bot_engines.get(i.engine_key)
        if eng and eng.is_running:
            continue
        if not _should_autoresume:
            # Skip the resume work this cycle — the watchdog will handle it
            # within 60s. The bot card will still show is_running=true from
            # the DB row; engine_running will read false, which is
            # accurate ("DB says yes, engine actually dead, watchdog en route").
            continue
        # Engine is dead — resume it
        if not _creds_loaded:
            from backend.utils.encryption import decrypt, DecryptError
            cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
            if cfg:
                try:
                    _kk = decrypt(cfg.kucoin_key_enc or "", user_id)
                    _ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
                    _kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
                except Exception:
                    pass
            _creds_loaded = True
        pairs = [p.strip() for p in (i.pairs or "BTC/USDT").split(",")]
        eng = futures_engine_registry.for_bot(user_id, i.engine_key)
        try:
            eng.start_futures(
                strategy_name=i.strategy_name, pairs=pairs, leverage=i.leverage or 10,
                mode=i.mode or "paper", timeframe=i.timeframe or "15m",
                stoploss=i.stoploss or -0.03, wallet=i.wallet or 1000,
                take_profit_pct=(i.takeprofit or 0.015) * 100,
                max_position_pct=(i.risk_pct or 5.0),
                strategy_id=i.strategy_id,
                kucoin_key=_kk, kucoin_secret=_ks, kucoin_passphrase=_kp,
                # Phase 3 — restore ARM config from the persisted instance row
                arm_enabled       = bool(getattr(i, "arm_enabled", False) or False),
                arm_tp1_close_pct = float(getattr(i, "arm_tp1_close_pct", 50.0) or 50.0),
                arm_be_mode       = str(getattr(i, "arm_be_mode", "leverage") or "leverage"),
                arm_be_buffer_pct = float(getattr(i, "arm_be_buffer_pct", 1.0) or 1.0),
                arm_trail_to_tp1  = bool(getattr(i, "arm_trail_to_tp1", True)
                                          if i.arm_trail_to_tp1 is not None else True),
                # Restore per-bot strategy overrides (session window + equal-
                # price threshold). Without this, auto-resume would silently
                # revert a "24/7 session" bot back to NY hours (12-21 UTC)
                # after backend restart.
                session_start_hr_utc = int(getattr(i, "session_start_hr_utc", 12) or 12),
                session_end_hr_utc   = int(getattr(i, "session_end_hr_utc",   21) or 21),
                equal_price_thresh   = float(getattr(i, "equal_price_thresh", 0.001) or 0.001),
                # Phase 9 — restore hedge mode on resume (None/legacy rows
                # normalise to "single" inside start_futures).
                position_mode        = str(getattr(i, "position_mode", "single") or "single"),
                # Restore the user's strategy flag toggles (CHoCH / LDC opts).
                strategy_flags       = _decode_strategy_flags(getattr(i, "strategy_flags", None)),
                # Restore SL/TP source so a "force slider" bot doesn't silently
                # revert to structural levels after a backend restart.
                force_slider_sltp    = bool(getattr(i, "force_slider_sltp", False)),
                # Restore paper-mode cost simulation across restarts.
                paper_sim_costs      = bool(getattr(i, "paper_sim_costs", False)),
                guard_enabled        = bool(getattr(i, "guard_enabled", True) if i.guard_enabled is not None else True),
                guard_max_consec     = int(getattr(i, "guard_max_consec", 5) or 5),
                guard_cooldown_min   = int(getattr(i, "guard_cooldown_min", 60) or 60),
            )
            log.info("Auto-resumed bot %s for user %s (ARM=%s)",
                     i.engine_key, user_id, getattr(i, "arm_enabled", False))
        except Exception as exc:
            log.warning("Failed to auto-resume bot %s: %s", i.engine_key, exc)
    # Record this attempt so the throttle gate skips the next ~30s.
    if _should_autoresume:
        _last_bots_autoresume_ts[user_id] = _now_resume
    # Refresh engine list after potential resumes
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}

    # Per-bot trade stats from DB. Previous implementation did 2
    # separate queries PER bot (count + sum), so N bots cost 2N
    # round trips — a major source of /bots latency that surfaced as
    # "Loading bots…" hanging in the UI for several seconds.
    #
    # Now: TWO consolidated queries total (regardless of bot count):
    #   1. count + sum grouped by (strategy_id, mode), bounded to
    #      trades opened on/after each instance's created_at
    #   2. open-position count per (strategy_id, mode) so the card
    #      reflects DB truth even when the bot engine isn't in memory
    #
    # The first query uses a UNION-style aggregate per instance because
    # the per-instance created_at cutoff makes a single GROUP BY
    # impossible. We instead do ONE call returning rows keyed by
    # (strategy_id, mode) for each instance's window. Even with 10
    # bots this is one DB hit instead of 20.
    from sqlalchemy import func, and_, or_ as _sql_or
    db_trade_counts: dict[int, int] = {}
    db_pnl_sums: dict[int, float] = {}
    db_open_counts: dict[int, int] = {}    # NEW: open-position fallback

    if instances:
        # Build a single query that handles ALL instances' windows.
        # We use a CASE-WHEN aggregation grouped by (strategy_id, mode)
        # PLUS we apply the latest created_at per (strategy_id, mode)
        # as the cutoff. Simpler approach: bulk-load all relevant
        # trades once, then bucket them in Python.
        relevant_strategy_ids = list({i.strategy_id for i in instances if i.strategy_id})
        relevant_modes        = list({i.mode for i in instances if i.mode})
        if relevant_strategy_ids and relevant_modes:
            min_created = min(i.created_at for i in instances if i.created_at)
            all_trades = db.execute(
                select(
                    Trade.id,
                    Trade.strategy_id,
                    Trade.mode,
                    Trade.entry_time,
                    Trade.profit_abs,
                    Trade.status,
                ).where(
                    Trade.user_id == user_id,
                    Trade.market_type == "futures",
                    Trade.strategy_id.in_(relevant_strategy_ids),
                    Trade.mode.in_(relevant_modes),
                    Trade.entry_time >= min_created,
                )
            ).all()
            # Bucket in Python — cheap, single pass.
            for i in instances:
                tot = 0
                pnl = 0.0
                opn = 0
                for tid, t_sid, t_mode, t_entry, t_profit, t_status in all_trades:
                    if t_sid != i.strategy_id or t_mode != i.mode:
                        continue
                    if t_entry is None or t_entry < i.created_at:
                        continue
                    tot += 1
                    if t_status == "closed":
                        pnl += float(t_profit or 0.0)
                    elif t_status == "open":
                        opn += 1
                db_trade_counts[i.id] = tot
                db_pnl_sums[i.id] = pnl
                db_open_counts[i.id] = opn

    bots = []
    for i in instances:
        eng = bot_engines.get(i.engine_key) if i.engine_key else None
        engine_running = eng.is_running if eng else False
        engine_status = eng.status if eng else None
        db_count = db_trade_counts.get(i.id, 0)
        eng_total = (engine_status or {}).get("total_trades", 0) + (engine_status or {}).get("open_trades", 0)
        winding = (engine_status or {}).get("winding_down", False)
        bots.append({
            "id": i.id,
            "strategy_name": i.strategy_name,
            "strategy_id": i.strategy_id,
            "mode": i.mode,
            "pairs": i.pairs,
            "leverage": i.leverage,
            "timeframe": i.timeframe,
            "wallet": i.wallet,
            "is_running": (i.is_running and engine_running) or winding,
            "winding_down": winding,
            "paused":         (engine_status or {}).get("paused", False),
            "engine_running": engine_running,
            "total_trades": eng_total or db_count or i.total_trades or 0,
            "closed_trades": (engine_status or {}).get("total_trades", i.total_trades or 0),
            # Prefer DB-summed P&L (always authoritative across restarts)
            # over engine_status.realized_pnl which is 0 after each Railway
            # redeploy. Engine value is used only as a sanity check.
            "total_pnl": round(
                db_pnl_sums.get(i.id, 0.0)
                or (engine_status or {}).get("realized_pnl", 0.0)
                or float(i.total_pnl or 0.0),
                4,
            ),
            # Engine-reported count is freshest, but falls back to the
            # DB count (just bulk-loaded above) when the engine isn't in
            # memory yet — e.g. right after a Railway restart, or while
            # the watchdog is mid-revive. Without the fallback the bot
            # card showed "Open: 0" even when DB had open positions for
            # the bot, contradicting what the user saw in the Positions
            # tab.
            "open_positions": (
                (engine_status or {}).get("open_trades")
                if engine_status is not None
                else db_open_counts.get(i.id, 0)
            ),
            "ticks": (engine_status or {}).get("ticks", 0),
            "signals": (engine_status or {}).get("signal_count", 0),
            "last_action": (engine_status or {}).get("last_action", ""),
            # Risk guardrail state (consecutive-loss adaptive cooldown)
            "guard_state": getattr(eng, "_guard_state", "active") if eng else "active",
            "guard_cooldown_until": (
                eng._guard_cooldown_until.isoformat()
                if eng and getattr(eng, "_guard_cooldown_until", None) else None
            ),
            "risk_pct": i.risk_pct,
            "stoploss": i.stoploss,
            "takeprofit": i.takeprofit,
            "position_mode": getattr(i, "position_mode", "single") or "single",
            "strategy_flags": _decode_strategy_flags(getattr(i, "strategy_flags", None)),
            "force_slider_sltp": bool(getattr(i, "force_slider_sltp", False)),
            "paper_sim_costs": bool(getattr(i, "paper_sim_costs", False)),
            "engine_key": i.engine_key,
            "created_at": str(i.created_at),
        })
    return {"bots": bots}


@router.post("/bots")
def create_futures_bot(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Create and start a new futures bot instance (supports multiple concurrent bots)."""
    strategy_id   = req.get("strategy_id")
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    mode          = req.get("mode", "paper")
    pairs         = req.get("pairs", ["BTC/USDT"])
    # ── PDF §7 hard safety caps (per "sane defaults" table) ───────────
    # Even if the UI's number inputs let the user type 99, the engine
    # clamps to safe bounds here so a typo / bug can't size a 100%-risk
    # bet at 50x leverage. These caps are stricter than KuCoin's own
    # platform limits — they protect the trader from themselves.
    leverage      = max(1, min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10))))
    timeframe     = req.get("timeframe", "15m")
    wallet        = max(10.0, min(1_000_000.0, float(req.get("wallet", 1000))))   # $10 floor / $1M ceiling
    stoploss      = max(-0.50, min(-0.001, float(req.get("stoploss", -0.03))))    # SL between -0.1% and -50%
    takeprofit    = max(0.001, min(2.0,    float(req.get("takeprofit", 0.015))))  # TP between 0.1% and 200%
    drawdown_tolerance = max(0.0, min(100.0, float(req.get("drawdown_tolerance", 50))))
    max_position_pct   = max(0.5, min(25.0,  float(req.get("max_position_pct", 5.0))))  # 0.5%-25% per trade

    arm_enabled       = bool(req.get("arm_enabled", False))
    arm_tp1_close_pct = max(1.0, min(99.0,   float(req.get("arm_tp1_close_pct", 50))))
    arm_be_mode       = str(req.get("arm_be_mode", "leverage"))
    if arm_be_mode not in ("leverage", "manual_pct", "entry"):
        arm_be_mode   = "leverage"
    arm_be_buffer_pct = max(0.0, min(10.0,   float(req.get("arm_be_buffer_pct", 1.0))))
    arm_trail_to_tp1  = bool(req.get("arm_trail_to_tp1", True))

    # Consecutive-loss adaptive cooldown guardrail (per-bot config)
    guard_enabled      = bool(req.get("guard_enabled", True))
    guard_max_consec   = max(2, min(20, int(req.get("guard_max_consec", 5))))
    guard_cooldown_min = max(5, min(1440, int(req.get("guard_cooldown_min", 60))))

    # Phase 8 — Cooldown / max-trades-per-day / daily DD trip.
    # Defaults follow the validator's TRADE_LIMIT_DEFAULT: when the
    # strategy doesn't declare its own max_trades_per_day, the bot uses
    # 999 (effectively unlimited) rather than imposing a mode-based cap
    # the user never asked for. Range-clamped only to a sane upper bound
    # (1000/day) to catch runaway-bug loops. The UI's "Max trades / day"
    # input still wins over both when the user fills it in.
    max_trades_per_day = max(1, min(1000, int(req.get("max_trades_per_day", 999))))
    cooldown_candles   = max(0, min(50,   int(req.get("cooldown_candles",   0))))
    max_daily_dd_pct   = max(5.0, min(80.0, float(req.get("max_daily_dd_pct", 25.0))))
    # Optional per-strategy risk gates (added 2026-05-24). Defaults to 0
    # = disabled, matching the engine's "no enforcement" stance. When the
    # user / strategy declares > 0, the engine adds the circuit breaker.
    # None (not in payload) → engine derives it from the strategy's
    # class_max_hold_candles (LDC=4 / Ash=60) for backtest parity. An explicit
    # int (incl. 0 = disable) overrides. The bot UI sends the Bar-hold value
    # inside strategy_flags (so it persists + auto-resumes), so this top-level
    # field is normally absent — keep it for API clients.
    _mh_raw            = req.get("max_hold_candles", None)
    max_hold_candles   = (max(0, min(5000, int(_mh_raw))) if _mh_raw is not None else None)
    max_stops_per_day  = max(0, min(100,  int(req.get("max_stops_per_day", 0))))

    # ── Session window + equal-price threshold (PDF §3, §6) ─────────
    # When the user picks a region in the UI we receive the UTC hour
    # range; SMCStrategy1 reads these via class attributes after the
    # engine wires them in. Falls back to defaults (NY session, 0.1%
    # threshold) when not provided.
    session_start_hr_utc  = max(0, min(23, int(req.get("session_start_hr_utc", 12))))
    session_end_hr_utc    = max(0, min(23, int(req.get("session_end_hr_utc", 21))))
    equal_price_thresh    = max(0.0001, min(0.05, float(req.get("equal_price_thresh_pct", 0.1)) / 100.0))

    # ── Position mode (Phase 9 — hedge support) ───────────────────────
    # "single" (default) = stop-and-reverse (TV default; pair nets to one
    # position). "hedge" = a LONG and a SHORT may coexist on the same pair.
    # Normalised to a known value so a bad payload can't change risk
    # behaviour; anything unexpected falls back to "single".
    position_mode = str(req.get("position_mode", "single") or "single").strip().lower()
    if position_mode not in ("single", "hedge"):
        position_mode = "single"

    # ── SL/TP source (added 2026-06-13) ───────────────────────────────
    # False (default) = structural-or-slider; True = force the slider %s for
    # every trade (live/paper equivalent of the backtest "From sliders below").
    force_slider_sltp = bool(req.get("force_slider_sltp", False))

    # ── Paper-mode cost simulation (added 2026-06-13) ─────────────────
    # Paper-only realism toggle. When True, the engine deducts simulated
    # KuCoin fees + slippage from paper P&L so paper tracks live. Ignored in
    # live mode (the exchange charges real fees there).
    paper_sim_costs = bool(req.get("paper_sim_costs", False))

    # ── Per-bot strategy flag overrides (UI toggles) ──────────────────
    # Sanitised to a flat dict of booleans so a bad payload can't inject
    # arbitrary state. JSON-stored on the row; passed as a dict to the engine.
    import json as _json_flags
    _raw_sf = req.get("strategy_flags")
    strategy_flags = None
    if isinstance(_raw_sf, dict) and _raw_sf:
        strategy_flags = {}
        for _k, _v in _raw_sf.items():
            # Preserve booleans AND numbers (e.g. max_hold_candles). bool is a
            # subclass of int, so check it first to avoid coercing True -> 1.
            if isinstance(_v, bool):
                strategy_flags[str(_k)] = _v
            elif isinstance(_v, (int, float)):
                strategy_flags[str(_k)] = _v
        strategy_flags = strategy_flags or None
    strategy_flags_json = _json_flags.dumps(strategy_flags) if strategy_flags else None

    strat = None
    if strategy_id:
        from backend.models.strategy import Strategy
        from sqlalchemy import or_
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name = strat.name

    # ── Phase 5d — Live Guardrail (PDF §9) ────────────────────────────
    # Block live trading unless:
    #   • Strategy compiled successfully (validator can read its code)
    #   • Confidence score ≥ 85
    #   • No critical missing fields (direction / entry_trigger / TF)
    #   • No detected logic conflicts (RSI direction reversed, etc.)
    # Paper mode bypasses the guard so users can safely experiment with
    # incomplete strategies before promoting to live.
    if mode == "live" and strat:
        try:
            from backend.services.strategy_validator import validate_for_live
            from backend.models.trade import FuturesBacktest
            live_ok, template, reason = validate_for_live(
                strategy_name       = strategy_name,
                strategy_id         = strategy_id,
                generated_code      = strat.generated_code or "",
                execution_timeframe = timeframe,
            )
            # Cache the validation result on the Strategy row so subsequent
            # checks don't re-run the validator. Updated every time the
            # user re-validates explicitly (POST /strategy/validate).
            try:
                strat.compiled_template = template.to_dict()
                strat.confidence_score  = template.confidence_score
                strat.live_permission   = template.live_permission
                db.commit()
            except Exception:
                pass

            # ── PDF §9 row 4: backtest-pass requirement ────────────────
            # Live trading is only allowed when a futures backtest has
            # been completed for (strategy, primary pair, timeframe) in
            # the last 30 days. This prevents users from going live with
            # a strategy that has never been tested on real data.
            primary_pair = (pairs or ["BTC/USDT"])[0]
            recent_bt = db.execute(
                select(FuturesBacktest)
                .where(
                    FuturesBacktest.user_id == user_id,
                    FuturesBacktest.strategy_id == strategy_id,
                    FuturesBacktest.pair == primary_pair,
                    FuturesBacktest.timeframe == timeframe,
                )
                .order_by(desc(FuturesBacktest.created_at))
                .limit(1)
            ).scalar_one_or_none()
            has_recent_backtest = bool(
                recent_bt and recent_bt.created_at and
                (datetime.utcnow() - recent_bt.created_at).days <= 30
            )

            # PDF §9 row 6 — "demo trading log on record". Count CLOSED paper
            # trades for this (strategy, pair) and require ≥ MIN. Default 5
            # is conservative; the user can lower via the request body to
            # 0 (opt-out) only when their bot create payload explicitly
            # passes `skip_paper_dwell: true` AND a successful backtest exists.
            MIN_PAPER_TRADES = 5
            paper_closed_count = db.execute(
                select(func.count(Trade.id)).where(
                    Trade.user_id == user_id,
                    Trade.strategy_id == strategy_id,
                    Trade.market_type == "futures",
                    Trade.mode == "paper",
                    Trade.pair == primary_pair,
                    Trade.status == "closed",
                )
            ).scalar() or 0
            has_paper_dwell = paper_closed_count >= MIN_PAPER_TRADES
            skip_paper_dwell = bool(req.get("skip_paper_dwell", False)) and has_recent_backtest

            if not live_ok or not has_recent_backtest or (not has_paper_dwell and not skip_paper_dwell):
                block_reason = reason or "Live guardrail blocked"
                if not has_recent_backtest:
                    block_reason += (
                        " | No backtest pass on record for this "
                        f"(strategy, {primary_pair}, {timeframe}) in the last 30 days. "
                        "Run a backtest first."
                    )
                if not has_paper_dwell and not skip_paper_dwell:
                    block_reason += (
                        f" | Only {paper_closed_count}/{MIN_PAPER_TRADES} closed paper trades on "
                        f"this strategy + {primary_pair}. Run the bot in PAPER mode first to "
                        "build a track record (or pass skip_paper_dwell=true if you've already "
                        "validated externally)."
                    )
                log_event(db, user_id, "futures.create_bot.blocked_live", request, payload={
                    "strategy": strategy_name, "reason": block_reason,
                    "confidence_score": template.confidence_score,
                    "live_permission":  template.live_permission,
                    "missing_fields":   template.missing_fields,
                    "conflicts":        template.conflicts,
                    "has_recent_backtest": has_recent_backtest,
                    "paper_closed_count": paper_closed_count,
                    "min_paper_trades":   MIN_PAPER_TRADES,
                })
                return {
                    "error":             block_reason,
                    "blocked_reason":    "live_guardrail",
                    "confidence_score":  template.confidence_score,
                    "live_permission":   template.live_permission,
                    "missing_fields":    template.missing_fields,
                    "inferred_fields":   template.inferred_fields,
                    "conflicts":         template.conflicts,
                    "has_recent_backtest":  has_recent_backtest,
                    "has_paper_dwell":      has_paper_dwell,
                    "paper_closed_count":   paper_closed_count,
                    "min_paper_trades":     MIN_PAPER_TRADES,
                    "resolver_notes":    template.resolver_notes,
                    "suggestion": (
                        "Step 1: Run a backtest. Step 2: Create a Paper-mode bot and "
                        f"let it close at least {MIN_PAPER_TRADES} trades. Step 3: Then go Live. "
                        "Live trading is blocked until confidence ≥ 85, a backtest exists, "
                        "AND the paper bot has a track record."
                    ),
                }
        except Exception as guard_exc:
            # Don't fail closed if validator itself errors — that would lock
            # out every live bot if a release breaks the validator. Log
            # loudly and continue. (The strategy_compile check on engine
            # start will still catch broken strategies.)
            log.warning(
                "[%s] Live guardrail validator raised: %s — allowing live start "
                "but flagging for manual review", user_id, guard_exc,
            )

    # ── Deduplication: stop any existing running bot for same strategy+pair+mode ──
    pairs_csv = ",".join(sorted(p.strip() for p in pairs))
    existing = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.user_id == user_id,
            StrategyInstance.strategy_name == strategy_name,
            StrategyInstance.pairs == pairs_csv,
            StrategyInstance.mode == mode,
            StrategyInstance.market_type == "futures",
            StrategyInstance.is_running == True,
        )
    ).scalars().all()
    for ex in existing:
        # Stop the old engine if running
        if ex.engine_key:
            bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
            old_eng = bot_engines.get(ex.engine_key)
            if old_eng and old_eng.is_running:
                old_eng._stop_evt.set()
        ex.is_running = False
    if existing:
        db.commit()
        log.info("[%s] Stopped %d duplicate bot(s) for %s/%s/%s",
                 user_id, len(existing), strategy_name, pairs_csv, mode)

    engine_key = f"bot-{strategy_name}-{int(_time.time() * 1000)}"
    instance = StrategyInstance(
        user_id=user_id, strategy_id=strategy_id, strategy_name=strategy_name,
        market_type="futures", mode=mode, pairs=pairs_csv,
        leverage=leverage, timeframe=timeframe, wallet=wallet,
        stoploss=stoploss, takeprofit=takeprofit, risk_pct=max_position_pct,
        is_running=True, engine_key=engine_key,
        # Phase 3 — persist ARM config so auto-resume keeps the same settings
        arm_enabled       = arm_enabled,
        arm_tp1_close_pct = arm_tp1_close_pct,
        arm_be_mode       = arm_be_mode,
        arm_be_buffer_pct = arm_be_buffer_pct,
        arm_trail_to_tp1  = arm_trail_to_tp1,
        # Per-bot strategy overrides (added 2026-05-24) — survive restart
        session_start_hr_utc = session_start_hr_utc,
        session_end_hr_utc   = session_end_hr_utc,
        equal_price_thresh   = equal_price_thresh,
        # Phase 9 — persist position mode so auto-resume restarts hedge
        # bots in hedge mode (not the safe-default "single").
        position_mode        = position_mode,
        # UI strategy flag toggles (JSON) — persisted for auto-resume.
        strategy_flags       = strategy_flags_json,
        # SL/TP source — persist so auto-resume keeps "force slider" on.
        force_slider_sltp    = force_slider_sltp,
        # Paper-mode cost realism — persist so auto-resume keeps it on.
        paper_sim_costs      = paper_sim_costs,
        # Consecutive-loss adaptive cooldown guardrail — persist for auto-resume.
        guard_enabled        = guard_enabled,
        guard_max_consec     = guard_max_consec,
        guard_cooldown_min   = guard_cooldown_min,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    # Resolve KuCoin credentials for live mode
    kk = ks = kp = ""
    if mode == "live":
        from backend.utils.encryption import decrypt, DecryptError
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            try:
                kk = decrypt(cfg.kucoin_key_enc or "", user_id)
                ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
                kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
            except Exception:
                pass

    # Start an ISOLATED engine for this bot (supports multiple concurrent bots).
    # Phase 3: pass ARM params so the engine wires partial-TP / BE-trail /
    # trail-to-TP1 behaviour on every position this bot opens. When
    # arm_enabled=False these are ignored.
    eng = futures_engine_registry.for_bot(user_id, engine_key)
    result = eng.start_futures(
        strategy_name=strategy_name, pairs=pairs, leverage=leverage,
        mode=mode, timeframe=timeframe, stoploss=stoploss,
        wallet=wallet, take_profit_pct=takeprofit * 100,
        max_position_pct=max_position_pct,
        strategy_id=strategy_id,
        kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
        arm_enabled        = arm_enabled,
        arm_tp1_close_pct  = arm_tp1_close_pct,
        arm_be_mode        = arm_be_mode,
        arm_be_buffer_pct  = arm_be_buffer_pct,
        arm_trail_to_tp1   = arm_trail_to_tp1,
        max_trades_per_day = max_trades_per_day,
        cooldown_candles   = cooldown_candles,
        max_daily_dd_pct   = max_daily_dd_pct,
        session_start_hr_utc = session_start_hr_utc,
        session_end_hr_utc   = session_end_hr_utc,
        equal_price_thresh   = equal_price_thresh,
        max_hold_candles     = max_hold_candles,
        max_stops_per_day    = max_stops_per_day,
        position_mode        = position_mode,
        force_slider_sltp    = force_slider_sltp,
        paper_sim_costs      = paper_sim_costs,
        strategy_flags       = strategy_flags,
        guard_enabled        = guard_enabled,
        guard_max_consec     = guard_max_consec,
        guard_cooldown_min   = guard_cooldown_min,
    )

    log_event(db, user_id, "futures.create_bot", request, payload={
        "instance_id": instance.id, "strategy": strategy_name, "leverage": leverage,
        "mode": mode, "max_position_pct": max_position_pct,
        "position_mode": position_mode,
        "strategy_flags": strategy_flags,
        "arm_enabled": arm_enabled,
        "arm_tp1_close_pct": arm_tp1_close_pct if arm_enabled else None,
        "arm_be_mode": arm_be_mode if arm_enabled else None,
        "arm_be_buffer_pct": arm_be_buffer_pct if arm_enabled else None,
        "arm_trail_to_tp1": arm_trail_to_tp1 if arm_enabled else None,
    })
    return {"bot_id": instance.id, "engine_key": engine_key, **result}


@router.post("/bots/{bot_id}/pause")
def pause_futures_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """NICE-6 — pause a futures bot.

    Block new entries while STILL managing open positions (TP/SL/liq/ARM).
    Different from DELETE /bots/{id} which winds down + stops the engine.
    Pause is fully reversible via POST /bots/{id}/resume."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}
    if not instance.engine_key:
        return {"error": "Bot has no engine key — cannot pause."}
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
    eng = bot_engines.get(instance.engine_key)
    if not eng or not eng.is_running:
        return {"error": "Bot engine is not running."}
    eng.pause()
    # Persist so auto-resume re-applies the paused flag after a backend
    # restart. Without this the bot silently un-pauses on every redeploy.
    instance.is_paused = True
    db.commit()
    log_event(db, user_id, "futures.pause_bot", request, payload={"bot_id": bot_id})
    return {"paused": True, "bot_id": bot_id}


@router.post("/bots/{bot_id}/resume")
def resume_futures_bot(
    bot_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Resume a paused bot — new entries re-enabled."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}
    if not instance.engine_key:
        return {"error": "Bot has no engine key — cannot resume."}
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
    eng = bot_engines.get(instance.engine_key)
    if not eng or not eng.is_running:
        return {"error": "Bot engine is not running."}
    eng.resume()
    instance.is_paused = False
    db.commit()
    log_event(db, user_id, "futures.resume_bot", request, payload={"bot_id": bot_id})
    return {"paused": False, "bot_id": bot_id}


@router.delete("/bots/{bot_id}")
def stop_futures_bot(
    bot_id: int,
    request: Request,
    force: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Stop a futures bot. If open positions exist, enters wind-down mode
    (manages TP/SL to exit profitably, no new entries). Use ?force=true to kill immediately."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}

    eng = None
    if instance.engine_key:
        bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
        eng = bot_engines.get(instance.engine_key)

    has_open = eng and eng.is_running and len(eng.positions) > 0

    if has_open and not force:
        eng.wind_down()
        log_event(db, user_id, "futures.wind_down_bot", request, payload={
            "bot_id": bot_id, "open_positions": len(eng.positions),
        })
        return {
            "stopped": False,
            "winding_down": True,
            "open_positions": len(eng.positions),
            "message": f"Bot has {len(eng.positions)} open position(s) — entering wind-down mode. "
                       "Engine will manage TP/SL exits and stop automatically when all positions close.",
        }

    # Immediate stop (no open positions or force=true)
    instance.is_running = False
    db.commit()

    if instance.engine_key:
        futures_engine_registry.stop_bot(user_id, instance.engine_key)
    else:
        eng_default = futures_engine_registry.for_user(user_id)
        eng_default.stop()

    log_event(db, user_id, "futures.stop_bot", request, payload={"bot_id": bot_id})
    return {"stopped": True, "bot_id": bot_id}


@router.get("/bots/{bot_id}/performance")
def futures_bot_performance(
    bot_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get performance metrics for a specific bot."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}

    # Only fetch trades belonging to THIS bot's strategy + mode, created
    # after the bot was started. The mode filter is CRITICAL — without it,
    # a paper bot's performance panel would also include any LIVE trades
    # made by other instances of the same strategy template (and vice
    # versa) → counts and PNL leak across paper/live boundary.
    trade_filter = [
        Trade.user_id == user_id, Trade.market_type == "futures",
        Trade.status == "closed",
    ]
    if instance.strategy_id:
        trade_filter.append(Trade.strategy_id == instance.strategy_id)
    if instance.created_at:
        trade_filter.append(Trade.entry_time >= instance.created_at)
    if instance.mode in ("paper", "live"):
        trade_filter.append(Trade.mode == instance.mode)

    # Fetch the LATEST 100 trades for display, but compute total_pnl and
    # win_rate from ALL trades so the inner panel matches the bot card
    # (which uses DB sums across the bot's full lifetime). Before this,
    # bots with >100 trades had inconsistent P&L between card and detail.
    trades = db.execute(
        select(Trade).where(*trade_filter).order_by(desc(Trade.exit_time)).limit(100)
    ).scalars().all()
    # Compute totals + wins via TWO simple queries — `func.case(...)` was
    # tried earlier but `else_` is a keyword on `sqlalchemy.case`, not on
    # `func.case`, so it errored with
    #   TypeError: Function.__init__() got an unexpected keyword argument 'else_'
    # visible in the UI as "Failed to load bot details".
    # Two queries are simpler and equally fast at this scale.
    agg_row = db.execute(
        select(
            func.coalesce(func.sum(Trade.profit_abs), 0.0),
            func.count(Trade.id),
        ).where(*trade_filter)
    ).one()
    total_pnl = float(agg_row[0] or 0)
    total_trade_count = int(agg_row[1] or 0)
    wins = db.execute(
        select(func.count(Trade.id)).where(
            *trade_filter, Trade.profit_abs > 0,
        )
    ).scalar() or 0
    win_rate = round(wins / total_trade_count * 100, 1) if total_trade_count else 0

    engine_data = {}
    winding_down = False
    if instance.engine_key:
        bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
        eng = bot_engines.get(instance.engine_key)
        if eng:
            s = eng.status
            winding_down = s.get("winding_down", False)
            engine_data = {
                "action_log": s.get("action_log", []),
                "open_positions_detail": s.get("open_positions_detail", []),
                "closed_trades_detail": s.get("closed_trades_detail", []),
                "balance": s.get("balance", 0),
                "ticks": s.get("ticks", 0),
                "signal_count": s.get("signal_count", 0),
                "last_action": s.get("last_action", ""),
                "unrealized_pnl": s.get("unrealized_pnl", 0),
                "realized_pnl": s.get("realized_pnl", 0),
            }

    # Extract signal criteria from strategy description/docstring
    signal_criteria = _extract_signal_criteria(instance.strategy_name, instance.strategy_id, db, user_id)

    return {
        "bot_id": bot_id,
        "strategy_name": instance.strategy_name,
        "total_trades": total_trade_count,
        "total_pnl": round(total_pnl, 4),
        "win_rate": win_rate,
        "is_running": instance.is_running,
        "winding_down": winding_down,
        "mode": instance.mode,
        "pairs": instance.pairs,
        "leverage": instance.leverage,
        "risk_pct": instance.risk_pct,
        # ── Configured-at-create-time fields surfaced to the UI ────────
        # Previously the running-bot panel showed leverage + mode + pairs
        # but NOT the timeframe or ARM config the user picked when they
        # clicked "Create (Paper)". This made it impossible to verify
        # at a glance whether the running bot was actually on the TF /
        # ARM setup the user intended. Surface all of it now so the UI
        # can render a "currently configured" summary.
        "timeframe":          instance.timeframe,
        "stoploss":           instance.stoploss,
        "takeprofit":         instance.takeprofit,
        "arm_enabled":        getattr(instance, "arm_enabled", False),
        "arm_tp1_close_pct":  getattr(instance, "arm_tp1_close_pct", 50.0),
        "arm_be_mode":        getattr(instance, "arm_be_mode", "leverage"),
        "arm_be_buffer_pct":  getattr(instance, "arm_be_buffer_pct", 1.0),
        "arm_trail_to_tp1":   getattr(instance, "arm_trail_to_tp1", True),
        # Per-bot strategy overrides (added 2026-05-24)
        "session_start_hr_utc": getattr(instance, "session_start_hr_utc", 12),
        "session_end_hr_utc":   getattr(instance, "session_end_hr_utc",   21),
        "equal_price_thresh":   getattr(instance, "equal_price_thresh",   0.001),
        # Position mode (Phase 9 — hedge support)
        "position_mode":        getattr(instance, "position_mode", "single") or "single",
        "strategy_flags":       _decode_strategy_flags(getattr(instance, "strategy_flags", None)),
        "force_slider_sltp":    bool(getattr(instance, "force_slider_sltp", False)),
        "paper_sim_costs":      bool(getattr(instance, "paper_sim_costs", False)),
        "signal_criteria": signal_criteria,
        "trades": [
            {
                "pair": t.pair, "direction": getattr(t, "side", "long"),
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "profit_abs": round(t.profit_abs or 0, 4),
                "profit_pct": round(t.profit_pct or 0, 2),
                "exit_reason": t.exit_reason,
                "entry_time": str(t.entry_time) if t.entry_time else None,
                "exit_time": str(t.exit_time) if t.exit_time else None,
                "leverage": t.leverage,
            }
            for t in trades[:20]
        ],
        **engine_data,
    }
