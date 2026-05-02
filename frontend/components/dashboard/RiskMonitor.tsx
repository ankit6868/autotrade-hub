'use client';

import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface RiskData {
  daily_pnl: number;
  paper_daily_pnl: number;
  live_daily_pnl: number;
  daily_drawdown_pct: number;
  max_daily_drawdown_pct: number;
  drawdown_used_pct: number;
  circuit_breaker_triggered: boolean;
  open_trades: number;
  max_open_trades: number;
  open_trades_used_pct: number;
  max_position_pct: number;
  auto_trade_running: boolean;
}

function GaugeBar({ value, label, color }: { value: number; label: string; color: string }) {
  const pct = Math.min(Math.max(value, 0), 100);
  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-xs">
        <span className="text-slate-400">{label}</span>
        <span className={`font-medium ${color}`}>{pct.toFixed(0)}%</span>
      </div>
      <div className="w-full h-2 rounded-full bg-[#2a3a52] overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${
            pct >= 80 ? 'bg-red-500' : pct >= 50 ? 'bg-yellow-500' : 'bg-emerald-500'
          }`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

export default function RiskMonitor() {
  const [data, setData] = useState<RiskData | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const d = await api.analysis.riskMonitor();
      if (!d.error) setData(d);
    } catch {}
    setLoading(false);
  }, []);

  useEffect(() => {
    refresh();
    const interval = setInterval(refresh, 30000); // refresh every 30s
    return () => clearInterval(interval);
  }, [refresh]);

  if (loading) {
    return (
      <div className="card">
        <h2 className="font-semibold text-sm text-slate-400 mb-3">🛡️ Risk Monitor</h2>
        <p className="text-slate-500 text-xs">Loading...</p>
      </div>
    );
  }

  if (!data) return null;

  return (
    <div className={`card border transition-colors ${
      data.circuit_breaker_triggered
        ? 'border-red-500/60 bg-red-500/5 animate-pulse'
        : 'border-[#2a3a52]'
    }`}>
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-semibold flex items-center gap-2">
          🛡️ Risk Monitor
          {data.auto_trade_running && (
            <span className="text-xs px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400">Engine ON</span>
          )}
        </h2>
        {data.circuit_breaker_triggered && (
          <span className="text-xs px-2 py-1 rounded bg-red-500/25 border border-red-500/40 text-red-400 font-semibold">
            ⛔ Circuit Breaker!
          </span>
        )}
      </div>

      {data.circuit_breaker_triggered && (
        <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30">
          <p className="text-red-400 text-xs font-medium">
            ⚠️ Daily drawdown limit reached. New trades are blocked until tomorrow.
          </p>
        </div>
      )}

      {/* Today's P&L */}
      <div className="grid grid-cols-3 gap-3 mb-4">
        <div className="text-center">
          <p className="text-xs text-slate-400">Today's P&L</p>
          <p className={`text-base font-bold ${data.daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {data.daily_pnl >= 0 ? '+' : ''}{data.daily_pnl.toFixed(2)}
          </p>
          <p className="text-xs text-slate-500">USDT</p>
        </div>
        <div className="text-center">
          <p className="text-xs text-slate-400">Paper</p>
          <p className={`text-base font-bold ${data.paper_daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {data.paper_daily_pnl >= 0 ? '+' : ''}{data.paper_daily_pnl.toFixed(2)}
          </p>
        </div>
        <div className="text-center">
          <p className="text-xs text-slate-400">Live</p>
          <p className={`text-base font-bold ${data.live_daily_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
            {data.live_daily_pnl >= 0 ? '+' : ''}{data.live_daily_pnl.toFixed(2)}
          </p>
        </div>
      </div>

      {/* Gauges */}
      <div className="space-y-3">
        <GaugeBar
          value={data.drawdown_used_pct}
          label={`Daily Drawdown (${data.daily_drawdown_pct.toFixed(1)}% / ${data.max_daily_drawdown_pct}% max)`}
          color={data.drawdown_used_pct >= 80 ? 'text-red-400' : data.drawdown_used_pct >= 50 ? 'text-yellow-400' : 'text-emerald-400'}
        />
        <GaugeBar
          value={data.open_trades_used_pct}
          label={`Open Trades (${data.open_trades} / ${data.max_open_trades} max)`}
          color={data.open_trades_used_pct >= 80 ? 'text-yellow-400' : 'text-emerald-400'}
        />
      </div>

      {/* Risk rules reminder */}
      <div className="mt-4 pt-3 border-t border-[#2a3a52]/50 grid grid-cols-2 gap-2 text-xs text-slate-400">
        <span>Max position: {data.max_position_pct}% per trade</span>
        <span>Max drawdown: {data.max_daily_drawdown_pct}% / day</span>
      </div>
    </div>
  );
}
