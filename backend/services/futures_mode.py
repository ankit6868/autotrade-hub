"""
Futures API mode resolver — the single source of truth for the ONE thing that
differs between KuCoin Lead (copy-trading) and Regular futures trading:

  1. the order-endpoint path
  2. which stored credentials to use

Everything else (account, positions, fills, market data, HMAC signing) is
identical between the two, so the rest of the stack stays shared.

`lead` is the default everywhere, so the existing Lead terminal is byte-for-byte
unchanged — a caller only gets Regular behaviour by explicitly passing
mode="regular" (the new /regular-futures-trade page sends X-Futures-Api: regular).
"""
from __future__ import annotations

LEAD = "lead"
REGULAR = "regular"

# Lead copy-trading order endpoints (visible to followers)
_LEAD_ORDERS = "/api/v1/copy-trade/futures/orders"
# Regular futures order endpoints (private account)
_REG_ORDERS = "/api/v1/orders"
_REG_ST_ORDERS = "/api/v1/st-orders"  # regular futures stop/trigger (TP/SL) orders


def normalize_mode(mode: str | None) -> str:
    """Coerce any caller-supplied value to 'lead' or 'regular' (default lead)."""
    return REGULAR if str(mode or "").strip().lower() in {"regular", "reg", "normal", "standard"} else LEAD


def is_regular(mode: str | None) -> bool:
    return normalize_mode(mode) == REGULAR


def orders_path(mode: str | None) -> str:
    """Order place/cancel/list endpoint for the given mode."""
    return _REG_ORDERS if is_regular(mode) else _LEAD_ORDERS


def st_orders_path(mode: str | None) -> str:
    """TP/SL trigger-order endpoint.

    Lead: KuCoin's Lead wrapper does NOT honour /st-orders as a queued trigger
    (it fires immediately) — TP/SL must go on the copy-trade orders endpoint with
    stop fields. Regular futures uses the real /api/v1/st-orders stop endpoint.
    """
    return _REG_ST_ORDERS if is_regular(mode) else _LEAD_ORDERS


def cancel_by_client_oid_path(mode: str | None, client_oid: str) -> str:
    """Regular futures cancels by clientOid via a path param; Lead via query."""
    if is_regular(mode):
        return f"{_REG_ORDERS}/client-order/{client_oid}"
    return f"{_LEAD_ORDERS}/client-order"


def cancel_by_order_id(mode: str | None, order_id: str) -> tuple[str, dict | None]:
    """(endpoint, query_params) to cancel ONE order by exchange orderId.

    Lead returns exactly today's shape — endpoint /copy-trade/futures/orders with
    ?orderId= — so the lead terminal is byte-identical. Regular futures cancels by
    a path param: DELETE /api/v1/orders/{orderId}.
    """
    if is_regular(mode):
        return f"{_REG_ORDERS}/{order_id}", None
    return _LEAD_ORDERS, {"orderId": order_id}


def cancel_by_client_oid(mode: str | None, symbol: str, client_oid: str) -> tuple[str, dict | None]:
    """(endpoint, query_params) to cancel by clientOid — lead shape unchanged."""
    if is_regular(mode):
        return f"{_REG_ORDERS}/client-order/{client_oid}", None
    return f"{_LEAD_ORDERS}/client-order", {"symbol": symbol, "clientOid": client_oid}


def load_kucoin_creds(cfg, user_id: str, mode: str | None) -> tuple[str, str, str]:
    """Decrypt the credential set for the requested mode.

    Returns (key, secret, passphrase). Raises DecryptError on failure so callers
    can surface the existing "re-enter in Setup" message.
    """
    from backend.utils.encryption import decrypt

    if is_regular(mode):
        return (
            decrypt(cfg.kucoin_reg_key_enc or "", user_id),
            decrypt(cfg.kucoin_reg_secret_enc or "", user_id),
            decrypt(cfg.kucoin_reg_passphrase_enc or "", user_id),
        )
    return (
        decrypt(cfg.kucoin_key_enc or "", user_id),
        decrypt(cfg.kucoin_secret_enc or "", user_id),
        decrypt(cfg.kucoin_passphrase_enc or "", user_id),
    )


def has_creds(cfg, mode: str | None) -> bool:
    """Whether the encrypted credential set for `mode` is present."""
    if cfg is None:
        return False
    if is_regular(mode):
        return bool(cfg.kucoin_reg_key_enc and cfg.kucoin_reg_secret_enc and cfg.kucoin_reg_passphrase_enc)
    return bool(cfg.kucoin_key_enc and cfg.kucoin_secret_enc and cfg.kucoin_passphrase_enc)
