'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

interface WebhookLog {
  id: number;
  event: string;
  mode: string;
  pair: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export default function WebhookManager() {
  const [configured, setConfigured] = useState(false);
  const [secret, setSecret] = useState<string | null>(null);
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [logs, setLogs] = useState<WebhookLog[]>([]);
  const [generating, setGenerating] = useState(false);
  const [copied, setCopied] = useState(false);
  const [showLogs, setShowLogs] = useState(false);

  useEffect(() => {
    api.webhook.secretStatus().then((d) => setConfigured(d.configured)).catch(() => {});
    loadLogs();
  }, []);

  async function loadLogs() {
    try {
      const d = await api.webhook.logs(20);
      setLogs(d.logs || []);
    } catch {}
  }

  async function generate() {
    setGenerating(true);
    try {
      const d = await api.webhook.generateSecret();
      setSecret(d.webhook_secret);
      // Build full URL using current host
      const base = window.location.origin.replace('3000', '8000'); // dev: backend on 8000
      setWebhookUrl(`${base}${d.webhook_url}`);
      setConfigured(true);
      loadLogs();
    } catch (e) {
      alert(String(e));
    }
    setGenerating(false);
  }

  async function copyUrl() {
    if (webhookUrl) {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    }
  }

  return (
    <div className="card">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="font-semibold flex items-center gap-2">
            🔗 TradingView Webhook
            <span className={`text-xs px-2 py-0.5 rounded-full ${configured ? 'bg-emerald-500/20 text-emerald-400' : 'bg-slate-500/20 text-slate-400'}`}>
              {configured ? 'Configured' : 'Not Set'}
            </span>
          </h2>
          <p className="text-slate-400 text-xs mt-1">
            Auto-trigger trades from TradingView alerts or any external signal source
          </p>
        </div>
        <button
          onClick={generate}
          disabled={generating}
          className="btn-secondary text-sm shrink-0"
        >
          {generating ? 'Generating...' : configured ? '🔄 Regenerate' : '⚡ Enable'}
        </button>
      </div>

      {/* Show newly generated secret */}
      {secret && (
        <div className="mb-4 p-3 rounded-lg bg-emerald-500/10 border border-emerald-500/30">
          <p className="text-emerald-400 text-xs font-semibold mb-2">
            ✅ Webhook enabled! Save this URL — the secret won&apos;t be shown again.
          </p>
          <div className="flex items-center gap-2">
            <code className="text-xs bg-[#0f1a2e] text-slate-300 px-3 py-2 rounded flex-1 break-all">
              {webhookUrl}
            </code>
            <button onClick={copyUrl} className="btn-secondary text-xs shrink-0">
              {copied ? '✅ Copied' : '📋 Copy'}
            </button>
          </div>
        </div>
      )}

      {/* TradingView alert JSON format guide */}
      <div className="mb-4 p-3 rounded-lg bg-[#0f1a2e] border border-[#2a3a52]">
        <p className="text-slate-400 text-xs font-medium mb-2">📋 TradingView Alert Message JSON:</p>
        <pre className="text-xs text-slate-300 overflow-x-auto whitespace-pre">{`{
  "action": "{{strategy.order.action}}",
  "pair":   "{{ticker}}",
  "price":  {{close}},
  "timeframe": "{{interval}}"
}`}</pre>
        <p className="text-slate-500 text-xs mt-2">
          Paste your webhook URL into TradingView → Alert → Notifications → Webhook URL
        </p>
      </div>

      {/* Supported actions */}
      <div className="flex flex-wrap gap-2 mb-4">
        {[
          { action: 'buy / long', color: 'bg-emerald-500/15 text-emerald-400', desc: 'Opens position' },
          { action: 'sell / close', color: 'bg-red-500/15 text-red-400', desc: 'Closes position' },
        ].map(({ action, color, desc }) => (
          <span key={action} className={`text-xs px-2 py-1 rounded-full border ${color} border-current/30`}>
            <strong>{action}</strong> — {desc}
          </span>
        ))}
      </div>

      {/* Logs toggle */}
      <button
        onClick={() => { setShowLogs(!showLogs); loadLogs(); }}
        className="text-xs text-slate-400 hover:text-white transition-colors"
      >
        {showLogs ? '▲ Hide' : '▼ Show'} recent webhook events ({logs.length})
      </button>

      {showLogs && logs.length > 0 && (
        <div className="mt-3 space-y-2 max-h-48 overflow-y-auto">
          {logs.map((log) => (
            <div key={log.id} className="flex items-start gap-2 p-2 rounded bg-[#0f1a2e] border border-[#2a3a52]/50 text-xs">
              <span className={`shrink-0 px-1.5 py-0.5 rounded text-xs ${
                log.event === 'webhook.trade_opened' ? 'bg-emerald-500/20 text-emerald-400' :
                log.event === 'webhook.trade_closed' ? 'bg-red-500/20 text-red-400' :
                'bg-slate-500/20 text-slate-400'
              }`}>
                {log.event.replace('webhook.', '')}
              </span>
              <span className="text-white font-medium">{log.pair || '—'}</span>
              <span className="text-slate-500 ml-auto shrink-0">
                {log.created_at ? new Date(log.created_at).toLocaleTimeString() : ''}
              </span>
            </div>
          ))}
        </div>
      )}
      {showLogs && logs.length === 0 && (
        <p className="mt-3 text-slate-500 text-xs">No webhook events yet</p>
      )}
    </div>
  );
}
