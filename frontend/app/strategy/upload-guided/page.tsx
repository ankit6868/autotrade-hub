'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';

/**
 * PDF §4.1 — Guided Strategy Upload Wizard.
 *
 * Structured form that posts to /api/strategy/upload-guided. Synthesizes
 * a complete IStrategy class on the backend without going through the
 * LLM. Lets non-technical users build strategies via dropdowns + sliders
 * while keeping the natural-language upload (LLM) path as the alternative.
 *
 * The form fields map 1:1 to the GuidedForm dataclass in
 * backend/services/guided_strategy_builder.py — keep these in sync.
 */

const TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '4h', '1d'];
const ENTRY_INDICATORS = [
  { value: 'rsi_threshold',  label: 'RSI threshold (e.g. < 30 / > 70)' },
  { value: 'macd_cross',     label: 'MACD crossover (signal line)' },
  { value: 'ema_cross',      label: 'EMA crossover (fast / slow)' },
  { value: 'bollinger_touch', label: 'Bollinger Band touch (mean-reversion)' },
];

export default function UploadGuidedPage() {
  const router = useRouter();
  const [name,             setName]             = useState('My Strategy');
  const [timeframe,        setTimeframe]        = useState('15m');
  const [direction,        setDirection]        = useState<'long'|'short'|'both'>('both');
  const [entryIndicator,   setEntryIndicator]   = useState('rsi_threshold');
  const [entryPeriod,      setEntryPeriod]      = useState(14);
  const [entryValue,       setEntryValue]       = useState(30);
  const [stoplossPct,      setStoplossPct]      = useState(1.5);
  const [takeProfitType,   setTakeProfitType]   = useState<'risk_reward'|'fixed_pct'>('risk_reward');
  const [riskReward,       setRiskReward]       = useState(2.0);
  const [takeProfitPct,    setTakeProfitPct]    = useState(3.0);
  const [riskPerTradePct,  setRiskPerTradePct]  = useState(0.5);
  const [biasFilter,       setBiasFilter]       = useState<'none'|'htf_ema200_up'>('none');
  const [biasTimeframes,   setBiasTimeframes]   = useState<string[]>([]);
  const [sessionFilter,    setSessionFilter]    = useState<'24h'|'ny'|'london'>('24h');
  const [armEnabled,       setArmEnabled]       = useState(false);
  const [armTp1ClosePct,   setArmTp1ClosePct]   = useState(50);
  const [submitting, setSubmitting] = useState(false);
  const [error,      setError]      = useState<string|null>(null);
  const [preview,    setPreview]    = useState<{code:string;id:number;confidence_score:number;live_permission:string}|null>(null);

  function toggleBiasTf(tf: string) {
    setBiasTimeframes(prev => prev.includes(tf) ? prev.filter(t => t !== tf) : [...prev, tf]);
  }

  async function submit() {
    setSubmitting(true); setError(null); setPreview(null);
    try {
      const r = await api.strategy.uploadGuided({
        name, timeframe, direction,
        entry_indicator: entryIndicator,
        entry_period:    entryPeriod,
        entry_value:     entryValue,
        stoploss_pct:    stoplossPct,
        take_profit_type: takeProfitType,
        risk_reward:     riskReward,
        take_profit_pct: takeProfitPct,
        risk_per_trade_pct: riskPerTradePct,
        bias_filter:     biasFilter,
        bias_timeframes: biasTimeframes,
        session_filter:  sessionFilter,
        arm_enabled:     armEnabled,
        arm_tp1_close_pct: armTp1ClosePct,
      });
      if (r?.error) { setError(r.error); }
      else {
        setPreview({
          code: r.code,
          id: r.id,
          confidence_score: r.confidence_score,
          live_permission:  r.live_permission,
        });
      }
    } catch (e: any) {
      setError(String(e?.message || e));
    }
    setSubmitting(false);
  }

  return (
    <div className="max-w-3xl mx-auto px-4 sm:px-6 py-8 space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-bold text-white">Guided Strategy Builder</h1>
        <p className="text-sm text-slate-400">
          Fill in the form and we synthesize a complete IStrategy class.
          No AI / LLM involved — deterministic + reproducible.
          Use the natural-language upload page if your rules don't fit these fields.
        </p>
      </header>

      <div className="card space-y-5">
        {/* Identity */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div>
            <label className="label">Strategy name</label>
            <input className="input" value={name} onChange={e => setName(e.target.value)} />
          </div>
          <div>
            <label className="label">Execution timeframe</label>
            <select className="input" value={timeframe} onChange={e => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map(tf => <option key={tf} value={tf}>{tf}</option>)}
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="label">Direction</label>
            <div className="inline-flex rounded-md border border-white/[0.06] overflow-hidden text-xs">
              {(['both','long','short'] as const).map(d => (
                <button key={d} type="button" onClick={() => setDirection(d)}
                  className={`px-3 py-1.5 ${direction===d ? 'bg-emerald-500/20 text-emerald-300' : 'text-slate-400 hover:bg-white/[0.04]'}`}>
                  {d === 'both' ? 'Long + Short' : d === 'long' ? 'Long only' : 'Short only'}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* Entry trigger */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 pt-2 border-t border-white/[0.04]">
          <div className="sm:col-span-3 text-xs font-semibold text-slate-300">Entry trigger</div>
          <div>
            <label className="label">Indicator</label>
            <select className="input" value={entryIndicator} onChange={e => setEntryIndicator(e.target.value)}>
              {ENTRY_INDICATORS.map(i => <option key={i.value} value={i.value}>{i.label}</option>)}
            </select>
          </div>
          <div>
            <label className="label">Period</label>
            <input type="number" className="input" value={entryPeriod} onChange={e => setEntryPeriod(parseInt(e.target.value||'14'))} />
          </div>
          <div>
            <label className="label">Threshold / value</label>
            <input type="number" step="0.5" className="input" value={entryValue} onChange={e => setEntryValue(parseFloat(e.target.value||'30'))} />
          </div>
        </div>

        {/* Risk */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-white/[0.04]">
          <div className="sm:col-span-2 text-xs font-semibold text-slate-300">Risk management</div>
          <div>
            <label className="label">Stop-loss (%)</label>
            <input type="number" step="0.1" className="input" value={stoplossPct} onChange={e => setStoplossPct(parseFloat(e.target.value||'1.5'))} />
          </div>
          <div>
            <label className="label">Risk per trade (% of wallet)</label>
            <input type="number" step="0.1" className="input" value={riskPerTradePct} onChange={e => setRiskPerTradePct(parseFloat(e.target.value||'0.5'))} />
          </div>
          <div>
            <label className="label">Take-profit type</label>
            <select className="input" value={takeProfitType} onChange={e => setTakeProfitType(e.target.value as any)}>
              <option value="risk_reward">Risk / Reward multiple</option>
              <option value="fixed_pct">Fixed %</option>
            </select>
          </div>
          <div>
            {takeProfitType === 'risk_reward' ? (
              <>
                <label className="label">Risk : Reward ratio</label>
                <input type="number" step="0.1" className="input" value={riskReward} onChange={e => setRiskReward(parseFloat(e.target.value||'2'))} />
              </>
            ) : (
              <>
                <label className="label">Take-profit (%)</label>
                <input type="number" step="0.1" className="input" value={takeProfitPct} onChange={e => setTakeProfitPct(parseFloat(e.target.value||'3'))} />
              </>
            )}
          </div>
        </div>

        {/* Filters */}
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-2 border-t border-white/[0.04]">
          <div className="sm:col-span-2 text-xs font-semibold text-slate-300">Optional filters</div>
          <div>
            <label className="label">HTF bias filter</label>
            <select className="input" value={biasFilter} onChange={e => setBiasFilter(e.target.value as any)}>
              <option value="none">None — trade both directions freely</option>
              <option value="htf_ema200_up">Trend filter — long-only above EMA200</option>
            </select>
          </div>
          <div>
            <label className="label">Trading session (UTC)</label>
            <select className="input" value={sessionFilter} onChange={e => setSessionFilter(e.target.value as any)}>
              <option value="24h">24h — no filter</option>
              <option value="ny">NY (12-21 UTC)</option>
              <option value="london">London (07-16 UTC)</option>
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="label">Multi-TF analyzer — bias timeframes</label>
            <p className="text-[10px] text-slate-500 mb-1">
              Strategy can read closed-candle data from these higher TFs (PDF §5). Must be strictly higher than execution TF.
            </p>
            <div className="flex flex-wrap gap-1.5">
              {['30m','1h','4h','1d'].filter(tf => tf !== timeframe).map(tf => (
                <button key={tf} type="button" onClick={() => toggleBiasTf(tf)}
                  className={`text-[11px] px-2 py-1 rounded border ${biasTimeframes.includes(tf) ? 'bg-purple-500/20 text-purple-300 border-purple-500/40' : 'text-slate-400 border-white/[0.06] hover:bg-white/[0.04]'}`}>
                  {tf}
                </button>
              ))}
            </div>
          </div>
        </div>

        {/* ARM */}
        <div className="pt-2 border-t border-white/[0.04]">
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={armEnabled} onChange={e => setArmEnabled(e.target.checked)} className="accent-purple-500" />
            <span className="text-xs font-semibold text-white">🎯 Advanced Risk Management (partial TP1 + BE trail)</span>
          </label>
          {armEnabled && (
            <div className="mt-2 ml-6">
              <label className="label">TP1 booking %</label>
              <input type="number" min={1} max={99} step={1} className="input w-24" value={armTp1ClosePct} onChange={e => setArmTp1ClosePct(parseInt(e.target.value||'50'))} />
            </div>
          )}
        </div>

        {error && <p className="text-red-400 text-xs">{error}</p>}

        <div className="flex items-center gap-2 pt-3">
          <button onClick={submit} disabled={submitting} className="btn-success">
            {submitting ? 'Building…' : 'Build + Save Strategy'}
          </button>
          {preview && (
            <button onClick={() => router.push(`/strategy/editor?id=${preview.id}`)} className="btn-secondary">
              Open in Editor →
            </button>
          )}
        </div>
      </div>

      {preview && (
        <div className="card space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">Strategy saved (#{preview.id})</h2>
            <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${
              preview.live_permission === 'live_eligible' ? 'bg-emerald-500/20 text-emerald-300'
              : preview.live_permission === 'demo_only' ? 'bg-amber-500/20 text-amber-300'
              : preview.live_permission === 'backtest_only' ? 'bg-sky-500/20 text-sky-300'
              : 'bg-red-500/20 text-red-300'
            }`}>
              {preview.confidence_score}/100 · {preview.live_permission}
            </span>
          </div>
          <pre className="text-[11px] text-slate-300 bg-[#131722] border border-white/[0.04] rounded p-3 overflow-x-auto max-h-96">
            {preview.code}
          </pre>
        </div>
      )}
    </div>
  );
}
