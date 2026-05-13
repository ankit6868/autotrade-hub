'use client';

import { setTokenProvider } from './api';

type MessageHandler = (data: Record<string, unknown>) => void;

// Token provider set by AuthBridge — same one used by HTTP requests
let _wsGetToken: (() => Promise<string | null>) | null = null;

export function setWsTokenProvider(fn: (() => Promise<string | null>) | null) {
  _wsGetToken = fn;
  // Keep api.ts in sync too
  setTokenProvider(fn);
}

class TradeWebSocket {
  private ws: WebSocket | null = null;
  private handlers: MessageHandler[] = [];
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;

  async connect() {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    // NEXT_PUBLIC_WS_URL takes priority (e.g. ws://localhost:8000 in dev).
    // Falls back to NEXT_PUBLIC_API_URL host, then Railway direct, then same-origin.
    let wsUrl: string;
    if (process.env.NEXT_PUBLIC_WS_URL) {
      wsUrl = `${process.env.NEXT_PUBLIC_WS_URL}/ws/trades`;
    } else if (process.env.NEXT_PUBLIC_API_URL) {
      const apiUrl = new URL(process.env.NEXT_PUBLIC_API_URL);
      const proto = apiUrl.protocol === 'https:' ? 'wss:' : 'ws:';
      wsUrl = `${proto}//${apiUrl.host}/ws/trades`;
    } else if (typeof window !== 'undefined' && window.location.hostname.includes('vercel.app')) {
      // Production on Vercel — connect directly to Railway backend
      wsUrl = 'wss://autotrade-backend-production.up.railway.app/ws/trades';
    } else {
      // Same-origin fallback for local dev
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      wsUrl = `${protocol}//${window.location.host}/ws/trades`;
    }

    // Attach token as query param — the WS endpoint reads ?token=
    try {
      if (_wsGetToken) {
        const token = await _wsGetToken();
        if (token) wsUrl += `?token=${encodeURIComponent(token)}`;
      }
    } catch {
      // No token — backend allows anonymous WS in local-dev mode
    }

    this.ws = new WebSocket(wsUrl);

    this.ws.onopen = () => {
      console.log('WebSocket connected');
      // Start pinging
      this.pingTimer = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send('ping');
        }
      }, 5000);
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.handlers.forEach((h) => h(data));
      } catch {
        // ignore
      }
    };

    this.ws.onclose = () => {
      console.log('WebSocket disconnected, reconnecting...');
      if (this.pingTimer) clearInterval(this.pingTimer);
      this.reconnectTimer = setTimeout(() => this.connect(), 3000);
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  disconnect() {
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.ws?.close();
    this.ws = null;
  }

  onMessage(handler: MessageHandler) {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((h) => h !== handler);
    };
  }
}

export const tradeWS = new TradeWebSocket();
