'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

/**
 * Daily risk limits for MANUAL LIVE futures trading. Applies to BOTH the
 * Futures (Lead) terminal and the Regular Futures terminal (per-user limit).
 * When a limit is hit the order endpoints block new live entries and the
 * trading panel shows a notification. Limits reset at 00:00 UTC = 5:30 AM IST.
 */
export default function DailyRiskManagement() {
  const [enabled, setEnabled] = useState(false);
  const [maxTrades, setMaxTrades] = useState(0);
  const [maxLosses, setMaxLosses] = useState(0);
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.config.status().then((s: { risk_daily_enabled?: boolean; risk_max_trades_per_day?: number; risk_max_losses_per_day?: number }) => {
      setEnabled(!!s?.risk_daily_enabled);
      setMaxTrades(Number(s?.risk_max_trades_per_day ?? 0));
      setMaxLosses(Number(s?.risk_max_losses_per_day ?? 0));
    }).catch(() => {});
  }, []);

  async function save() {
    setBusy(true);
    setSaved(false);
    try {
      await api.config.update({
        risk_daily_enabled: enabled,
        risk_max_trades_per_day: Math.max(0, Math.floor(maxTrades) || 0),
        risk_max_losses_per_day: Math.max(0, Math.floor(maxLosses) || 0),
      });
      setSaved(true);
    } catch {
      /* surfaced by the UI staying unsaved */
    }
    setBusy(false);
  }

  return (
    <div className="card mt-6">
      <h2 className="text-xl font-semibold mb-1">Daily Risk Management</h2>
      <p className="text-slate-400 text-sm mb-5">
        Auto-pause your <span className="text-white">manual live</span> trading when a daily limit is hit — on both the
        Futures Terminal and the Regular Futures terminal. Limits reset every day at{' '}
        <span className="text-white">5:30 AM IST</span>. Toggle it off any time to resume.
      </p>

      <label className="flex items-center gap-3 mb-4 cursor-pointer">
        <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} className="w-4 h-4 accent-emerald-500" />
        <span className="text-sm text-white font-medium">Enable daily risk limits</span>
      </label>

      <div className={`space-y-4 ${enabled ? '' : 'opacity-50 pointer-events-none'}`}>
        <div>
          <label className="label">Max trades per day <span className="text-slate-500">(0 = no limit)</span></label>
          <input className="input" type="number" min={0} value={maxTrades} onChange={e => setMaxTrades(Number(e.target.value))} placeholder="e.g. 10" />
          <p className="text-[11px] text-slate-500 mt-1">After this many live entries today, new trades are blocked with a notification.</p>
        </div>
        <div>
          <label className="label">Max losses per day <span className="text-slate-500">(0 = no limit)</span></label>
          <input className="input" type="number" min={0} value={maxLosses} onChange={e => setMaxLosses(Number(e.target.value))} placeholder="e.g. 3" />
          <p className="text-[11px] text-slate-500 mt-1">After this many losing trades today, new trades are blocked with a notification.</p>
        </div>
      </div>

      <div className="flex gap-3 mt-5">
        <button onClick={save} disabled={busy} className="btn-primary">{busy ? 'Saving…' : 'Save Risk Settings'}</button>
      </div>
      {saved && <div className="mt-3 p-3 rounded-lg text-sm bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">Risk settings saved.</div>}
    </div>
  );
}
