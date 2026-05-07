/* eslint-disable @typescript-eslint/no-explicit-any */
const API_BASE = process.env.NEXT_PUBLIC_API_URL || '';

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
  const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(text || `HTTP ${res.status}`);
  }
  return res.json();
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
      const res = await fetch(`${API_BASE}/api/strategy/upload`, { method: 'POST', body: formData, headers });
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
    forceClose: (pair: string) =>
      request<any>(`/api/futures/force-close/${pair}`, { method: 'POST' }),
    manualEntry: (pair: string, direction: 'long' | 'short' = 'long', stakePct = 5) =>
      request<any>('/api/futures/manual-entry', {
        method: 'POST',
        body: JSON.stringify({ pair, direction, stake_pct: stakePct }),
      }),
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
