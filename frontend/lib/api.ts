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

async function request<T = any>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...((options?.headers as Record<string, string>) || {}),
  };
  if (_getToken) {
    try {
      const token = await _getToken();
      if (token) headers.Authorization = `Bearer ${token}`;
    } catch {
      // Anonymous request — backend allows when Clerk isn't configured.
    }
  }

  // First try: same-origin (Vercel rewrite → Railway). This avoids carrier
  // blocks on *.railway.app domains.
  try {
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }
    return res.json();
  } catch (e: unknown) {
    // Network-level failure (DNS, TCP, TLS, carrier block). Only retry on
    // TypeError — actual HTTP error responses already threw above with a
    // body, no point retrying those.
    const isNetworkError = e instanceof TypeError;
    const sameAsLongBase = API_BASE === LONG_REQUEST_BASE;
    if (!isNetworkError || sameAsLongBase || !LONG_REQUEST_BASE) {
      throw e;
    }
    // Fall back to direct Railway in case Vercel itself is unreachable.
    // Rare, but useful belt-and-braces against carrier blocks on
    // *.vercel.app domains too.
    const res = await fetch(`${LONG_REQUEST_BASE}${path}`, { ...options, headers });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(text || `HTTP ${res.status}`);
    }
    return res.json();
  }
}

export const api = {
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
  },

  backtest: {
    run: (data: Record<string, unknown>) =>
      request<any>('/api/backtest/run', { method: 'POST', body: JSON.stringify(data) }),
    results: (id: number) => request<any>(`/api/backtest/results/${id}`),
  },

  trade: {
    start: (data: Record<string, unknown>) =>
      request<any>('/api/trade/start', { method: 'POST', body: JSON.stringify(data) }),
    stop: () => request<any>('/api/trade/stop', { method: 'POST' }),
    status: () => request<any>('/api/trade/status'),
    open: (mode?: 'paper' | 'live') =>
      request<{ trades: any[] }>(`/api/trade/open${mode ? `?mode=${mode}` : ''}`),
    history: (params?: Record<string, string>) => {
      const qs = params ? '?' + new URLSearchParams(params).toString() : '';
      return request<{ trades: any[] }>(`/api/trade/history${qs}`);
    },
    forceClose: (id: string | number) => request<any>(`/api/trade/force-close/${id}`, { method: 'POST' }),
    balance: () => request<any>('/api/trade/balance'),
    emergencyStop: () => request<any>('/api/trade/emergency-stop', { method: 'POST' }),
    manualEntry: (pair: string, direction: 'long' | 'short' = 'long', stake = 0) =>
      request<any>('/api/trade/manual-entry', {
        method: 'POST',
        body: JSON.stringify({ pair, direction, stake }),
      }),
  },

  market: {
    pairs: () => request<{ pairs: string[] }>('/api/market/pairs'),
    price: (pair: string) => request<any>(`/api/market/price/${pair}`),
    candles: (pair: string, type?: string) =>
      request<{ candles: any[] }>(`/api/market/candles/${pair}?kline_type=${type || '15min'}`),
    ohlcv: (pair: string, timeframe?: string, limit?: number) =>
      request<{ pair: string; candles: Array<{time:number;open:number;high:number;low:number;close:number;volume:number}> }>(
        `/api/market/ohlcv/${pair}?timeframe=${timeframe || '15m'}&limit=${limit || 120}`
      ),
    signals: (pair: string, interval?: string) =>
      request<any>(`/api/market/signals/${pair}?interval=${interval || '15m'}`),
  },

  analysis: {
    universe: () => request<{
      default_pairs: string[];
      strategies: { name: string; label: string; one_liner: string; ideal_timeframes: string[] }[];
    }>('/api/analysis/universe'),
    opportunities: (params?: { timeframe?: string; top_n?: number; min_score?: number; pairs?: string; strategies?: string }) => {
      const qs = params ? '?' + new URLSearchParams(params as Record<string, string>).toString() : '';
      return request<{
        timeframe: string;
        scanned_pairs: number;
        failed_pairs: string[];
        stale_pairs: string[];
        strategies_considered: string[];
        opportunities: any[];
        tv_status: {
          cache_entries: number;
          fresh_entries: number;
          stale_entries: number;
          cooldown_remaining_s: number;
        };
      }>(`/api/analysis/opportunities${qs}`);
    },
    analyze: (pair: string, timeframe = '15m') =>
      request<any>(`/api/analysis/analyze/${pair}?timeframe=${timeframe}`),
    topVolume: (n = 50) => request<{
      pairs: { pair: string; volume_usd: number; change_pct: number; price: number }[];
    }>(`/api/analysis/top-volume?n=${n}`),
    portfolio: () => request<any>('/api/analysis/portfolio'),
    riskMonitor: () => request<any>('/api/analysis/risk-monitor'),
  },

  webhook: {
    generateSecret: () => request<any>('/api/webhook/generate-secret', { method: 'POST' }),
    secretStatus: () => request<any>('/api/webhook/secret'),
    logs: (limit = 50) => request<any>(`/api/webhook/logs?limit=${limit}`),
  },

  autotrade: {
    status: () => request<any>('/api/autotrade/status'),
    start: () => request<any>('/api/autotrade/start', { method: 'POST' }),
    stop: () => request<any>('/api/autotrade/stop', { method: 'POST' }),
    settings: {
      get: () => request<any>('/api/autotrade/settings'),
      put: (data: Record<string, unknown>) =>
        request<any>('/api/autotrade/settings', { method: 'PUT', body: JSON.stringify(data) }),
    },
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
      run: (data: Record<string, unknown>) =>
        request<any>('/api/futures/backtest/run', { method: 'POST', body: JSON.stringify(data) }),
      history: (limit = 20) =>
        request<any>(`/api/futures/backtest/history?limit=${limit}`),
    },
    forceClose: (pair: string, mode?: 'paper' | 'live') =>
      request<any>(`/api/futures/force-close/${pair}`, {
        method: 'POST',
        body: JSON.stringify({ mode }),
      }),
    manualEntry: (pair: string, direction: 'long' | 'short' = 'long', stakePct = 5, leverage?: number, mode?: 'paper' | 'live') =>
      request<any>('/api/futures/manual-entry', {
        method: 'POST',
        body: JSON.stringify({ pair, direction, stake_pct: stakePct, ...(leverage ? { leverage } : {}), ...(mode ? { mode } : {}) }),
      }),
    orderbook: (symbol: string) => request<any>(`/api/futures/orderbook/${symbol}`),
    recentTrades: (symbol: string) => request<any>(`/api/futures/trades/${symbol}`),
    contracts: () => request<any>('/api/futures/contracts'),
    placeOrder: (data: Record<string, unknown>) =>
      request<any>('/api/futures/order', { method: 'POST', body: JSON.stringify(data) }),
    cancelOrder: (orderId: string) =>
      request<any>(`/api/futures/order/${orderId}`, { method: 'DELETE' }),
    orders: (params?: { symbol?: string; status?: string }) => {
      const qs = params ? '?' + new URLSearchParams(params as Record<string, string>).toString() : '';
      return request<any>(`/api/futures/orders${qs}`);
    },
    ordersHistory: (params?: { symbol?: string; limit?: number }) => {
      const qs = params ? '?' + new URLSearchParams(params as Record<string, string>).toString() : '';
      return request<any>(`/api/futures/orders/history${qs}`);
    },
    setLeverage: (data: { symbol: string; leverage: number }) =>
      request<any>('/api/futures/leverage', { method: 'POST', body: JSON.stringify(data) }),
    setMarginMode: (data: { symbol: string; mode: string }) =>
      request<any>('/api/futures/margin-mode', { method: 'POST', body: JSON.stringify(data) }),
    getLeverage: (symbol: string) => request<any>(`/api/futures/leverage/${symbol}`),
    setTpSl: (data: { pair: string; tp_price?: number; sl_price?: number }) =>
      request<any>('/api/futures/position/tp-sl', { method: 'POST', body: JSON.stringify(data) }),
    leadTradingStatus: () => request<any>('/api/futures/lead-trading-status'),
    bots: {
      list: (mode?: 'paper' | 'live') =>
        request<any>(`/api/futures/bots${mode ? `?mode=${mode}` : ''}`),
      create: (data: Record<string, unknown>) =>
        request<any>('/api/futures/bots', { method: 'POST', body: JSON.stringify(data) }),
      stop: (botId: number, force?: boolean) =>
        request<any>(`/api/futures/bots/${botId}${force ? '?force=true' : ''}`, { method: 'DELETE' }),
      performance: (botId: number) => request<any>(`/api/futures/bots/${botId}/performance`),
    },
  },

  copy: {
    becomeMaster: (strategy_id?: number) =>
      request<any>('/api/copy/become-master', { method: 'POST', body: JSON.stringify({ strategy_id }) }),
    mySignals: (limit = 50) => request<any>(`/api/copy/my-signals?limit=${limit}`),
    myFollowers: () => request<any>('/api/copy/my-followers'),
    subscribe: (data: Record<string, unknown>) =>
      request<any>('/api/copy/subscribe', { method: 'POST', body: JSON.stringify(data) }),
    unsubscribe: (masterId: string) =>
      request<any>(`/api/copy/unsubscribe/${masterId}`, { method: 'DELETE' }),
    mySubscriptions: () => request<any>('/api/copy/my-subscriptions'),
    feed: (limit = 50) => request<any>(`/api/copy/feed?limit=${limit}`),
  },

  multiStrategy: {
    list: () => request<any>('/api/strategies/instances'),
    create: (data: Record<string, unknown>) =>
      request<any>('/api/strategies/instances', { method: 'POST', body: JSON.stringify(data) }),
    stop: (id: number) =>
      request<any>(`/api/strategies/instances/${id}`, { method: 'DELETE' }),
    status: () => request<any>('/api/strategies/instances/status'),
  },

  bulkBacktest: (data: Record<string, unknown>) =>
    request<any>('/api/backtest/bulk', { method: 'POST', body: JSON.stringify(data) }),

  health: () => request<any>('/api/health'),
};
