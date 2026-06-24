/* eslint-disable @typescript-eslint/no-explicit-any */

// All regular API calls go same-origin so the browser only ever talks to the
// Vercel domain. Vercel's `rewrites` in vercel.json proxy /api/* to Railway
// server-side. Why this matters:
//
//   • Some mobile carriers (notably Indian ones — Jio, Airtel, Vi) block or
//     throttle the *.up.railway.app domain on their LTE networks. Direct
//     calls fail with Safari's generic "TypeError: Load failed" before
//     reaching the backend at all. WiFi works fine because the route is
//     different. Routing through autotrade-hub.vercel.app sidesteps the
//     carrier's block list entirely.
//   • Smaller TLS handshake — Vercel CDN is much closer to the user than
//     Railway's Singapore region.
//
// The one exception is `strategy.upload`: large multipart bodies can hit
// Vercel's edge-proxy ROUTER_EXTERNAL_TARGET_ERROR. That function uses
// LONG_REQUEST_BASE (direct Railway) below.
const RAILWAY_BACKEND = 'https://autotrade-backend-production.up.railway.app';

function resolveApiBase(): string {
  // Explicit override always wins (Docker, custom domain, etc.).
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  // Same-origin everywhere else — Vercel rewrites + Next dev rewrites both
  // handle it.
  return '';
}

const API_BASE = resolveApiBase();

// Direct Railway URL for endpoints that can't go through Vercel's edge
// proxy (large bodies, long-running requests). Falls back to same-origin
// in local dev so Next's rewrite still works.
function resolveLongRequestBase(): string {
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== 'undefined' && window.location.hostname.includes('vercel.app')) {
    return RAILWAY_BACKEND;
  }
  return '';
}

const LONG_REQUEST_BASE = resolveLongRequestBase();

// Set by AuthBridge once Clerk is loaded; lets us attach the user's JWT to
// every backend request without dragging React context into this module.
let _getToken: (() => Promise<string | null>) | null = null;

export function setTokenProvider(fn: (() => Promise<string | null>) | null) {
  _getToken = fn;
}

// Clerk-verified email, set by AuthBridge. Sent as X-User-Email so the backend
// can resolve the admin / unlimited allowlists even before the email is added
// to the Clerk session-token claim. (The token claim, when present, wins
// server-side.)
let _userEmail: string | null = null;
export function setUserEmail(email: string | null) {
  _userEmail = email;
}

// Retry policy. Mobile-data clients often abort TCP after ~15s, but Railway's
// first response after the container scales from zero takes 10-30s. The retry
// keeps the user-perceived latency in check while still surviving cold starts.
const FETCH_TIMEOUT_MS = 45_000;
const RETRY_BACKOFF_MS = [300, 1200, 3000];  // 3 retries, ~4.5s total

async function _fetchWithTimeout(url: string, options: RequestInit, timeoutMs: number) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: ctrl.signal });
  } finally {
    clearTimeout(t);
  }
}

// "Network-level error" = DNS/TCP/TLS/CORS-preflight failure or our own
// abort. These are the ones worth retrying; an HTTP 4xx/5xx is the server's
// considered response and retrying won't help.
function _isRetryableNetworkError(e: unknown): boolean {
  if (e instanceof TypeError) return true;                           // fetch network fail
  if (e && typeof e === 'object' && 'name' in e) {
    const name = (e as { name?: string }).name;
    if (name === 'AbortError' || name === 'TimeoutError') return true;
  }
  return false;
}

// Cheap structural check for "the backend says my JWT is no good" — we
// retry the request once with a freshly-minted token before bubbling up.
function _isAuthExpiredResponse(status: number, body: string): boolean {
  if (status !== 401 && status !== 403) return false;
  const b = body.toLowerCase();
  return b.includes('token expired') || b.includes('jwt expired')
      || b.includes('invalid token') || b.includes('unauthorized');
}

async function _tryOnce(base: string, path: string, options: RequestInit) {
  const res = await _fetchWithTimeout(`${base}${path}`, options, FETCH_TIMEOUT_MS);
  if (!res.ok) {
    const text = await res.text();
    // Tag with status so the caller can distinguish auth-expiry from other failures.
    const err: Error & { status?: number; rawBody?: string } = new Error(text || `HTTP ${res.status}`);
    err.status = res.status;
    err.rawBody = text;
    throw err;
  }
  return res.json();
}

async function _buildAuthHeaders(extra?: Record<string, string>): Promise<Record<string, string>> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(extra || {}),
  };
  if (_getToken) {
    try {
      const token = await _getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    } catch {
      // Anonymous request — backend allows when Clerk isn't configured.
    }
  }
  if (_userEmail) headers['X-User-Email'] = _userEmail;
  return headers;
}

async function request<T = any>(path: string, options?: RequestInit): Promise<T> {
  let headers = await _buildAuthHeaders(options?.headers as Record<string, string>);
  const buildOpts = (h: Record<string, string>): RequestInit => ({ ...options, headers: h });
  let finalOpts: RequestInit = buildOpts(headers);

  // Try same-origin (Vercel rewrite → Railway) up to N times with backoff.
  // Retries handle: Railway cold-start, transient edge routing flaps, and
  // mobile-carrier TCP resets. Each retry has the full FETCH_TIMEOUT_MS budget.
  let lastErr: unknown;
  let authRetried = false;
  for (let attempt = 0; attempt <= RETRY_BACKOFF_MS.length; attempt++) {
    try {
      return await _tryOnce(API_BASE, path, finalOpts) as T;
    } catch (e) {
      lastErr = e;
      // ── Auth-expiry retry ───────────────────────────────────────
      // If the backend said the JWT is expired/invalid, ask Clerk for
      // a brand-new token and retry once. This handles the common case
      // where the cached Clerk session token aged past its ~60s TTL
      // between the time the user opened the page and clicked the
      // button (Run Backtest, etc.).
      const status = (e as { status?: number })?.status;
      const body   = (e as { rawBody?: string })?.rawBody || (e as Error)?.message || '';
      if (!authRetried && _getToken && _isAuthExpiredResponse(status ?? 0, body)) {
        authRetried = true;
        headers = await _buildAuthHeaders(options?.headers as Record<string, string>);
        finalOpts = buildOpts(headers);
        continue;   // same attempt count; try again with fresh token
      }
      if (!_isRetryableNetworkError(e)) throw e;          // hard failure — surface immediately
      if (attempt < RETRY_BACKOFF_MS.length) {
        await new Promise(r => setTimeout(r, RETRY_BACKOFF_MS[attempt]));
      }
    }
  }

  // All same-origin attempts exhausted. Fall back to direct Railway once —
  // useful if Vercel itself can't reach the backend, e.g. a regional outage.
  if (LONG_REQUEST_BASE && LONG_REQUEST_BASE !== API_BASE) {
    try {
      return await _tryOnce(LONG_REQUEST_BASE, path, finalOpts) as T;
    } catch (e) {
      lastErr = e;
    }
  }

  // Out of options — replace the cryptic "TypeError: Load failed" with
  // something the user can act on.
  if (_isRetryableNetworkError(lastErr)) {
    throw new Error(
      'Could not reach the backend. The server may be waking up — try again ' +
      'in a few seconds. If this keeps happening switch networks (mobile data ↔ WiFi).'
    );
  }
  throw lastErr;
}

// ── Long-running request path ───────────────────────────────────────────
// Backtests can take 30s-4min (KuCoin data download + simulation across
// hundreds of thousands of candles). Vercel's edge proxy kills any
// rewrite that takes >60s with the cryptic ROUTER_EXTERNAL_TARGET_ERROR
// the user has hit multiple times. Railway's proxy gives us 5 minutes,
// which is enough for 1m × 30-day or 5m × 6-month combos.
//
// `requestLongRunning` bypasses the same-origin (Vercel) path entirely
// and goes DIRECT to Railway when we're on Vercel, with a 4-minute
// fetch timeout. Local dev uses the same same-origin path as `request`
// (Next.js dev rewrite goes straight to localhost:8000, no proxy in
// the way). Returns the same shape as `request<T>`.
const LONG_REQUEST_TIMEOUT_MS = 240_000;   // 4 minutes

async function requestLongRunning<T = any>(path: string, options?: RequestInit): Promise<T> {
  const headers = await _buildAuthHeaders(options?.headers as Record<string, string>);
  const finalOpts: RequestInit = { ...options, headers };
  // Pick the URL: Railway direct on Vercel, same-origin in local dev.
  const base = (typeof window !== 'undefined' && window.location.hostname.includes('vercel.app'))
    ? RAILWAY_BACKEND
    : API_BASE;
  try {
    const res = await _fetchWithTimeout(`${base}${path}`, finalOpts, LONG_REQUEST_TIMEOUT_MS);
    if (!res.ok) {
      const text = await res.text();
      const err: Error & { status?: number; rawBody?: string } = new Error(text || `HTTP ${res.status}`);
      err.status = res.status;
      err.rawBody = text;
      throw err;
    }
    return await res.json() as T;
  } catch (e) {
    if (_isRetryableNetworkError(e)) {
      throw new Error(
        'Backend timed out (>4 min) or could not be reached. For high-frequency '
        + 'timeframes (1m, 5m) try a shorter period. For long periods (1Y+) try '
        + '15m or 1h.'
      );
    }
    throw e;
  }
}

// Fire-and-forget warm-up: wakes Railway when the user first lands on the
// app so the *real* first request (Setup → Test Connection, etc.) doesn't
// pay the cold-start penalty. Cheap unauthenticated GET.
export function warmupBackend() {
  if (typeof window === 'undefined') return;
  _fetchWithTimeout(`${API_BASE}/api/health`, { method: 'GET' }, 10_000)
    .catch(() => {
      // ignore — if same-origin fails, try direct Railway so the container
      // wakes either way.
      if (LONG_REQUEST_BASE && LONG_REQUEST_BASE !== API_BASE) {
        _fetchWithTimeout(`${LONG_REQUEST_BASE}/api/health`, { method: 'GET' }, 10_000)
          .catch(() => { /* ignore */ });
      }
    });
}

if (typeof window !== 'undefined') {
  // Kick off warm-up immediately on module load.
  warmupBackend();
  // Re-ping every 4 minutes while the tab is open so Railway never gets a
  // chance to idle-scale back to zero between user actions.
  setInterval(warmupBackend, 4 * 60_000);
}

export const api = {
  access: {
    status: () => request<{
      active: boolean; tier: 'admin' | 'unlimited' | 'subscription' | 'none' | 'error_fail_open';
      is_admin: boolean; kind: string | null; expires_at: string | null;
      email?: string; email_seen?: boolean;
    }>('/api/access/status'),
    redeem: (code: string) =>
      request<any>('/api/access/redeem', { method: 'POST', body: JSON.stringify({ code }) }),
    // admin only
    listCodes: () => request<{ codes: any[] }>('/api/access/codes'),
    generate: (kind: 'subscription' | 'unlimited', count: number, duration_days?: number, note?: string) =>
      request<any>('/api/access/codes', { method: 'POST', body: JSON.stringify({ kind, count, duration_days, note }) }),
    revoke: (code: string, revoked = true) =>
      request<any>('/api/access/codes/revoke', { method: 'POST', body: JSON.stringify({ code, revoked }) }),
    // admin: user management
    listUsers: () => request<{ users: any[]; allowlist: any[] }>('/api/access/users'),
    extendUser: (user_id: string, days = 0, months = 0) =>
      request<any>('/api/access/users/extend', { method: 'POST', body: JSON.stringify({ user_id, days, months }) }),
    changeUserCode: (user_id: string, new_code?: string) =>
      request<any>('/api/access/users/change-code', { method: 'POST', body: JSON.stringify({ user_id, new_code }) }),
    pauseUser: (user_id: string, paused: boolean) =>
      request<any>('/api/access/users/pause', { method: 'POST', body: JSON.stringify({ user_id, paused }) }),
    revokeUser: (user_id: string) =>
      request<any>('/api/access/users/revoke', { method: 'POST', body: JSON.stringify({ user_id }) }),
    // admin: recent Clerk sign-ups + give-code-and-email
    listSignups: () => request<{
      error?: string;
      signups: { user_id: string; email: string; created_at: number; has_code: boolean; is_allowlisted: boolean }[];
    }>('/api/access/signups'),
    giveCode: (email: string, kind: 'subscription' | 'unlimited' = 'subscription', days?: number) =>
      request<{ ok: boolean; code?: string; kind?: string; emailed?: boolean; email_error?: string | null; error?: string }>(
        '/api/access/give-code', { method: 'POST', body: JSON.stringify({ email, kind, days }) }),
  },

  config: {
    setup: (data: Record<string, unknown>) =>
      request<any>('/api/config/setup', { method: 'POST', body: JSON.stringify(data) }),
    status: () => request<any>('/api/config/status'),
    update: (data: Record<string, unknown>) =>
      request<any>('/api/config/update', { method: 'PUT', body: JSON.stringify(data) }),
    testKucoin: () => request<any>('/api/config/test-kucoin', { method: 'POST' }),
    testOpenrouter: () => request<any>('/api/config/test-openrouter', { method: 'POST' }),
    models: () => request<{ models: { id: string; name: string; context_length: number }[] }>('/api/config/models'),
  },

  strategy: {
    upload: async (formData: FormData): Promise<any> => {
      const headers: Record<string, string> = {};
      if (_getToken) {
        try {
          const token = await _getToken();
          if (token) headers.Authorization = `Bearer ${token}`;
        } catch { /* anonymous */ }
      }
      // Upload uses direct Railway URL — Vercel edge proxy throws
      // ROUTER_EXTERNAL_TARGET_ERROR on multipart bodies > a few MB.
      const res = await fetch(`${LONG_REQUEST_BASE}/api/strategy/upload`, { method: 'POST', body: formData, headers });
      const raw = await res.text();
      // Try to parse as JSON first
      try {
        const parsed = JSON.parse(raw);
        // FastAPI wraps unhandled errors as {"detail": "..."} — normalise to {"error": "..."}
        if (parsed && parsed.detail && !parsed.error) {
          return { error: parsed.detail };
        }
        return parsed;
      } catch {
        return { error: raw || `HTTP ${res.status}` };
      }
    },
    parse: (data: { text: string; model?: string }) =>
      request<any>('/api/strategy/parse', { method: 'POST', body: JSON.stringify(data) }),
    validate: (data: { code: string }) =>
      request<{ valid: boolean; errors: string[] }>('/api/strategy/validate', { method: 'POST', body: JSON.stringify(data) }),
    aiAssist: (data: { prompt: string; existing_code: string; model?: string }) =>
      request<any>('/api/strategy/ai-assist', { method: 'POST', body: JSON.stringify(data) }),
    list: () => request<{ strategies: any[] }>('/api/strategy/list'),
    templates: () => request<{ templates: { file: string; name: string; code: string }[] }>('/api/strategy/templates'),
    get: (id: number) => request<any>(`/api/strategy/${id}`),
    update: (id: number, data: Record<string, unknown>) =>
      request<any>(`/api/strategy/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
    delete: (id: number) => request<any>(`/api/strategy/${id}`, { method: 'DELETE' }),
    dedupe: () => request<any>('/api/strategy/dedupe', { method: 'POST' }),
    // Re-run the strategy's stored natural-language text through the LLM
    // with the latest strict prompt — converts a config-only stub into a
    // complete IStrategy class with populate_* methods.
    regenerate: (id: number) =>
      request<any>(`/api/strategy/${id}/regenerate`, { method: 'POST', body: JSON.stringify({}) }),
    // PDF §4.1 — guided wizard upload (no LLM)
    uploadGuided: (form: Record<string, unknown>) =>
      request<any>('/api/strategy/upload-guided', { method: 'POST', body: JSON.stringify(form) }),
  },

  // Futures-only market data — `candles` and `signals` were removed
  // alongside the spot stack (only used by the deleted SignalsPanel).
  market: {
    pairs: () => request<{ pairs: string[] }>('/api/market/pairs'),
    price: (pair: string) => request<any>(`/api/market/price/${pair}`),
    ohlcv: (pair: string, timeframe?: string, limit?: number) =>
      request<{ pair: string; candles: Array<{time:number;open:number;high:number;low:number;close:number;volume:number}> }>(
        `/api/market/ohlcv/${pair}?timeframe=${timeframe || '15m'}&limit=${limit || 120}`
      ),
  },

  futures: {
    start: (data: Record<string, unknown>) =>
      request<any>('/api/futures/start', { method: 'POST', body: JSON.stringify(data) }),
    stop: () => request<any>('/api/futures/stop', { method: 'POST' }),
    status: () => request<any>('/api/futures/status'),
    open: (mode?: 'paper' | 'live') =>
      request<{ trades: any[] }>(`/api/futures/open${mode ? `?mode=${mode}` : ''}`),
    history: (params?: Record<string, string>) => {
      const qs = params ? '?' + new URLSearchParams(params).toString() : '';
      return request<{ trades: any[] }>(`/api/futures/history${qs}`);
    },
    balance: () => request<any>('/api/futures/balance'),
    account: (mode?: 'paper' | 'live') =>
      request<any>(`/api/futures/account${mode ? `?mode=${mode}` : ''}`),
    backtest: {
      // run + autoTune go through the long-running path: direct to Railway
      // (5min proxy timeout) instead of through Vercel's edge proxy (60s).
      // This is what lets 1m × 30-day or 5m × 6-month combos complete
      // without ROUTER_EXTERNAL_TARGET_ERROR.
      run: (data: Record<string, unknown>) =>
        requestLongRunning<any>('/api/futures/backtest/run', { method: 'POST', body: JSON.stringify(data) }),
      autoTune: (data: Record<string, unknown>) =>
        requestLongRunning<any>('/api/futures/backtest/auto-tune', { method: 'POST', body: JSON.stringify(data) }),
      // Run the same strategy + settings across several timeframes and return
      // a side-by-side comparison (exploration tool — pick a robust TF, don't
      // cherry-pick the best number). Long-running: each TF is a full backtest.
      timeframeSweep: (data: Record<string, unknown>) =>
        requestLongRunning<any>('/api/futures/backtest/timeframe-sweep', { method: 'POST', body: JSON.stringify(data) }),
      // Out-of-sample robustness: splits the period into N windows, runs the
      // strategy on each, returns per-window results + a robust/fragile verdict.
      walkForward: (data: Record<string, unknown>) =>
        requestLongRunning<any>('/api/futures/backtest/walk-forward', { method: 'POST', body: JSON.stringify(data) }),
      history: (limit = 20) =>
        request<any>(`/api/futures/backtest/history?limit=${limit}`),
    },
    // Live verified results — REAL closed-trade P&L per bot (not backtests).
    dashboard: () => request<{
      today_pnl: { paper: number; live: number };
      total_pnl: { paper: number; live: number };
      bots: { strategy: string; mode: string; trades: number; win_rate: number; total_pnl: number; today_pnl: number }[];
      equity_curve: { t: string; pnl: number }[];
      history: { strategy: string; mode: string; pair: string; side: string; profit_abs: number; profit_pct: number; exit_reason: string; exit_time: string | null }[];
      trade_count: number;
    }>('/api/futures/dashboard'),
    forceClose: (
      pair: string,
      mode?: 'paper' | 'live',
      direction?: 'long' | 'short',
      positionId?: string,
    ) =>
      request<any>(`/api/futures/force-close/${pair}`, {
        method: 'POST',
        // position_id targets ONE specific row (engine or DB-fallback).
        // Without it the backend falls back to (pair, direction, mode)
        // matching, which closes EVERY position sharing those keys —
        // exactly the "Close = Close All" bug the user reported.
        body: JSON.stringify({
          mode,
          ...(direction ? { direction } : {}),
          ...(positionId ? { position_id: positionId } : {}),
        }),
      }),
    manualEntry: (
      pair: string,
      direction: 'long' | 'short' = 'long',
      stakePct = 5,
      leverage?: number,
      mode?: 'paper' | 'live',
      costUsdt?: number,
      allowHedge?: boolean,
      tpPrice?: number,
      slPrice?: number,
    ) =>
      request<any>('/api/futures/manual-entry', {
        method: 'POST',
        // cost_usdt is the user's typed margin in USDT. Backend prefers it
        // over stake_pct for live mode (stake_pct gets misinterpreted against
        // the engine's paper wallet, not the real KuCoin balance). Paper
        // mode keeps using stake_pct since there's no real exchange call.
        // allow_hedge=true lets long + short coexist on the same pair.
        //
        // tp_price/sl_price are OPTIONAL. They are sent ONLY when the user
        // explicitly set them — the backend no longer auto-adds stops the
        // user didn't ask for (bug 4). Omitting them = "no stop".
        body: JSON.stringify({
          pair, direction,
          stake_pct: stakePct,
          ...(costUsdt && costUsdt > 0 ? { cost_usdt: costUsdt } : {}),
          ...(leverage ? { leverage } : {}),
          ...(mode ? { mode } : {}),
          ...(allowHedge ? { allow_hedge: true } : {}),
          ...(tpPrice && tpPrice > 0 ? { tp_price: tpPrice } : {}),
          ...(slPrice && slPrice > 0 ? { sl_price: slPrice } : {}),
        }),
      }),
    orderbook: (symbol: string) => request<any>(`/api/futures/orderbook/${symbol}`),
    recentTrades: (symbol: string) => request<any>(`/api/futures/trades/${symbol}`),
    contracts: () => request<any>('/api/futures/contracts'),
    placeOrder: (data: Record<string, unknown>) =>
      request<any>('/api/futures/order', { method: 'POST', body: JSON.stringify(data) }),
    cancelOrder: (orderId: string) =>
      request<any>(`/api/futures/order/${orderId}`, { method: 'DELETE' }),
    orders: (params?: { symbol?: string; status?: string; mode?: 'paper' | 'live' }) => {
      const qs = params ? '?' + new URLSearchParams(params as Record<string, string>).toString() : '';
      return request<any>(`/api/futures/orders${qs}`);
    },
    ordersHistory: (params?: { symbol?: string; limit?: number; mode?: 'paper' | 'live' }) => {
      const qs = params ? '?' + new URLSearchParams(params as Record<string, string>).toString() : '';
      return request<any>(`/api/futures/orders/history${qs}`);
    },
    setLeverage: (data: { symbol: string; leverage: number }) =>
      request<any>('/api/futures/leverage', { method: 'POST', body: JSON.stringify(data) }),
    setMarginMode: (data: { symbol: string; mode: string }) =>
      request<any>('/api/futures/margin-mode', { method: 'POST', body: JSON.stringify(data) }),
    getLeverage: (symbol: string) => request<any>(`/api/futures/leverage/${symbol}`),
    // direction + position_id keep TP/SL targeting honest in hedge mode
    // (long + short on the same pair) — without them the backend would
    // attach the stops to whichever side iterated first.
    setTpSl: (data: {
      pair: string;
      tp_price?: number;
      sl_price?: number;
      direction?: 'long' | 'short';
      position_id?: string;
    }) =>
      request<any>('/api/futures/position/tp-sl', { method: 'POST', body: JSON.stringify(data) }),
    // Phase 5e — decoded strategy preview (rules + risk + confidence)
    // Used by the bot Create flow to render the "Strategy understood: X/100"
    // panel BEFORE the user picks Live.
    strategyPreview: (strategyId: number, timeframe: string) =>
      request<any>(`/api/strategy/${strategyId}/preview?timeframe=${encodeURIComponent(timeframe)}`),
    // UX#15 — TF-mismatch warning (strategy's authored TF vs user's pick)
    strategyTfCheck: (strategyId: number, executionTf: string) =>
      request<any>(`/api/strategy/${strategyId}/tf-check?execution_tf=${encodeURIComponent(executionTf)}`),
    // Phase 6 — partial close (e.g. book 50% then leave remainder).
    // position_id targets the specific row clicked; without it the backend
    // picks the first engine-side match which may not be the user's row.
    partialClose: (data: {
      pair: string;
      mode: 'paper' | 'live';
      close_pct: number;
      position_id?: string;
      direction?: 'long' | 'short';
    }) =>
      request<any>('/api/futures/position/partial-close', { method: 'POST', body: JSON.stringify(data) }),
    // Add margin to an open position (paper + live). Paper deducts from
    // engine balance, live calls KuCoin /api/v1/position/margin/deposit-margin.
    // direction + position_id target the exact hedge-mode side.
    addMargin: (data: {
      pair: string;
      mode: 'paper' | 'live';
      amount: number;
      direction?: 'long' | 'short';
      position_id?: string;
    }) =>
      request<any>('/api/futures/position/add-margin', { method: 'POST', body: JSON.stringify(data) }),
    // Reduce margin (paper only — live partial-close is the workaround).
    reduceMargin: (data: {
      pair: string;
      mode: 'paper';
      amount: number;
      direction?: 'long' | 'short';
      position_id?: string;
    }) =>
      request<any>('/api/futures/position/reduce-margin', { method: 'POST', body: JSON.stringify(data) }),
    // Top up virtual USDT in paper mode (main engine or specific bot).
    paperAddFunds: (data: { amount: number; reset?: boolean }) =>
      request<any>('/api/futures/paper/add-funds', { method: 'POST', body: JSON.stringify(data) }),
    paperBotAddFunds: (botId: number, data: { amount: number; reset?: boolean }) =>
      request<any>(`/api/futures/paper/bot/${botId}/add-funds`, { method: 'POST', body: JSON.stringify(data) }),
    // Cleanup endpoint — removes trades with entry==exit and profit_abs==0
    // (artifacts of the stale-price-cache bug fixed in ed1e7c7).
    cleanupBrokenTrades: (mode?: 'paper' | 'live') => {
      const q = mode ? `?mode=${mode}` : '';
      return request<any>(`/api/futures/cleanup-broken-trades${q}`, { method: 'DELETE' });
    },
    // Phase 6 — cancel-all endpoints
    cancelAllOrders: (params?: { mode?: 'paper' | 'live'; symbol?: string }) => {
      const q = new URLSearchParams();
      if (params?.mode)   q.set('mode', params.mode);
      if (params?.symbol) q.set('symbol', params.symbol);
      const qs = q.toString();
      return request<any>(`/api/futures/orders/all${qs ? `?${qs}` : ''}`, { method: 'DELETE' });
    },
    leadTradingStatus: () => request<any>('/api/futures/lead-trading-status'),
    // NICE-4: per-TF risk config (FR-04) — defaults + user overrides
    riskConfig: {
      get: () => request<any>('/api/futures/risk-config'),
      put: (overrides: Record<string, any>) =>
        request<any>('/api/futures/risk-config', { method: 'PUT', body: JSON.stringify({ overrides }) }),
    },
    bots: {
      list: (mode?: 'paper' | 'live') =>
        request<any>(`/api/futures/bots${mode ? `?mode=${mode}` : ''}`),
      create: (data: Record<string, unknown>) =>
        request<any>('/api/futures/bots', { method: 'POST', body: JSON.stringify(data) }),
      stop: (botId: number, force?: boolean) =>
        request<any>(`/api/futures/bots/${botId}${force ? '?force=true' : ''}`, { method: 'DELETE' }),
      // NICE-6: pause / resume — manages open positions but blocks new entries
      pause:  (botId: number) => request<any>(`/api/futures/bots/${botId}/pause`,  { method: 'POST' }),
      resume: (botId: number) => request<any>(`/api/futures/bots/${botId}/resume`, { method: 'POST' }),
      performance: (botId: number) => request<any>(`/api/futures/bots/${botId}/performance`),
    },
  },

  health: () => request<any>('/api/health'),
};
