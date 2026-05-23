'use client';
import { useEffect, useRef, useState } from 'react';
import { api } from '@/lib/api';

/**
 * Phase 5e — Decoded Strategy Preview panel.
 *
 * Renders the StrategyTemplate returned by GET /api/strategy/{id}/preview:
 *   • Confidence score with colour-coded band (live_eligible / demo_only / backtest_only / blocked)
 *   • Decoded conditions grouped by role
 *   • Risk plan (SL type, TP type, RR)
 *   • Trade limits (max trades/day, cooldown)
 *   • Missing / inferred fields and conflicts
 *   • Resolver notes (the human-readable audit trail)
 *
 * Shown above the Create button in BotCreateFlow so the user sees exactly
 * what the bot will trade with before clicking Create. When mode=live and
 * confidence < 85, the Create button is auto-disabled by the parent.
 */

interface Condition {
  role:        string;
  source:      string;
  timeframe:   string;
  indicator?:  string;
  period?:     number;
  rule?:       string;
  value?:      number;
  description?: string;
}

interface Template {
  strategy_name:       string;
  description?:        string;
  mode?:               string;
  direction?:          string;
  original_timeframe?: string;
  execution_timeframe: string;
  conditions:          Condition[];
  risk: {
    stop_loss_type?:     string;
    take_profit_type?:   string;
    risk_reward?:        number;
    risk_per_trade_pct?: number;
    source?:             string;
  };
  trade_limits: {
    max_trades_per_day?:  number;
    cooldown_candles?:    number;
    require_fresh_trigger?: boolean;
    max_concurrent?:      number;
  };
  confidence_score:    number;
  live_permission:     string;   // 'live_eligible' | 'demo_only' | 'backtest_only' | 'blocked'
  missing_fields:      string[];
  inferred_fields:     string[];
  conflicts:           string[];
  resolver_notes:      string[];
}

interface Props {
  strategyId: number | null;
  timeframe:  string;
  mode:       'paper' | 'live';
  /** Called whenever the preview loads, so the parent can disable Create on live block. */
  onPermissionChange?: (perm: string, score: number, liveOk: boolean) => void;
}

const PERMISSION_COLORS: Record<string, { bg: string; text: string; ring: string; label: string }> = {
  live_eligible:  { bg: 'bg-emerald-500/10', text: 'text-emerald-300', ring: 'ring-emerald-500/30', label: 'Live eligible' },
  demo_only:      { bg: 'bg-amber-500/10',   text: 'text-amber-300',   ring: 'ring-amber-500/30',   label: 'Demo / paper only' },
  backtest_only:  { bg: 'bg-sky-500/10',     text: 'text-sky-300',     ring: 'ring-sky-500/30',     label: 'Backtest only' },
  blocked:        { bg: 'bg-red-500/10',     text: 'text-red-300',     ring: 'ring-red-500/30',     label: 'Live trading blocked' },
};

const ROLE_LABELS: Record<string, string> = {
  bias_filter:       'Bias filter (HTF direction)',
  trend_filter:      'Trend filter',
  setup_filter:      'Setup filter',
  entry_trigger:     'Entry trigger',
  exit_signal:       'Exit / Structural SL/TP',
  session_filter:    'Session filter',
  volatility_filter: 'Volatility filter',
};

const SOURCE_LABELS: Record<string, { text: string; color: string }> = {
  user_strategy:          { text: 'from strategy', color: 'text-emerald-400' },
  inferred_from_strategy: { text: 'inferred',      color: 'text-amber-400' },
  inferred_for_execution: { text: 'auto-added',    color: 'text-sky-400' },
  default_safe:           { text: 'default',       color: 'text-slate-500' },
};

export default function StrategyPreview({ strategyId, timeframe, mode, onPermissionChange }: Props) {
  const [tpl,     setTpl]     = useState<Template | null>(null);
  const [loading, setLoading] = useState(false);
  const [error,   setError]   = useState<string | null>(null);

  // Hold the callback in a ref so it stays referentially stable across
  // renders. Parents commonly pass an inline arrow function
  // (`onPermissionChange={(...)=>{...}}`) — putting that in the fetch
  // effect's deps caused the fetch to re-run on EVERY parent render,
  // which flashed "Compiling strategy…" intermittently in BotPanel.
  // The actual fetch now only re-runs when strategyId or timeframe
  // changes, which is what we actually want.
  const onPermissionChangeRef = useRef(onPermissionChange);
  useEffect(() => { onPermissionChangeRef.current = onPermissionChange; }, [onPermissionChange]);

  useEffect(() => {
    if (!strategyId) return;
    setLoading(true); setError(null);
    let cancelled = false;
    api.futures.strategyPreview(strategyId, timeframe)
      .then((data: any) => {
        if (cancelled) return;
        if (data?.error) {
          setError(data.error);
          setTpl(null);
        } else {
          setTpl(data as Template);
          onPermissionChangeRef.current?.(
            data.live_permission,
            data.confidence_score,
            data.live_permission === 'live_eligible' && data.confidence_score >= 85,
          );
        }
      })
      .catch((e: any) => { if (!cancelled) setError(String(e?.message || e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [strategyId, timeframe]);

  if (!strategyId)  return null;
  if (loading)      return <div className="p-3 text-[10px] text-slate-500">Compiling strategy…</div>;
  if (error)        return <div className="p-3 text-[10px] text-red-400">Preview failed: {error}</div>;
  if (!tpl)         return null;

  const perm  = PERMISSION_COLORS[tpl.live_permission] || PERMISSION_COLORS.blocked;
  const score = tpl.confidence_score;
  const conditionsByRole: Record<string, Condition[]> = {};
  for (const c of tpl.conditions) {
    (conditionsByRole[c.role] = conditionsByRole[c.role] || []).push(c);
  }

  const isLiveBlocked = mode === 'live' && tpl.live_permission !== 'live_eligible';

  return (
    <div className={`rounded-lg border p-2.5 ${perm.bg} ring-1 ${perm.ring} space-y-2.5`}>
      {/* ── Header: score + permission band ──────────────────────────── */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[10px] text-slate-400">Strategy understanding</span>
          <span className={`text-base font-bold ${perm.text}`}>{score}/100</span>
        </div>
        <span className={`text-[9px] font-bold px-2 py-0.5 rounded-full ${perm.bg} ${perm.text} ring-1 ${perm.ring}`}>
          {perm.label}
        </span>
      </div>

      {/* ── Live block banner ────────────────────────────────────────── */}
      {isLiveBlocked && (
        <div className="text-[10px] text-red-300 bg-red-500/10 border border-red-500/30 rounded px-2 py-1.5 leading-snug">
          🛑 Live trading is blocked for this strategy.
          Run a backtest first, paper-trade for a few days, then enable Live once
          confidence reaches 85+. The settings here can still be saved as a paper bot.
        </div>
      )}

      {/* ── Conditions grouped by role ───────────────────────────────── */}
      {tpl.conditions.length > 0 && (
        <div>
          <div className="text-[10px] text-slate-400 font-semibold mb-1">Decoded rules</div>
          <div className="space-y-1">
            {Object.entries(conditionsByRole).map(([role, conds]) => (
              <div key={role} className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1">
                <div className="text-[10px] text-slate-300 font-medium">{ROLE_LABELS[role] || role}</div>
                <div className="flex flex-wrap gap-1 mt-0.5">
                  {conds.map((c, i) => {
                    const src = SOURCE_LABELS[c.source] || { text: c.source, color: 'text-slate-500' };
                    return (
                      <span
                        key={i}
                        className="inline-flex items-center gap-1 text-[10px] bg-[#1e222d] border border-white/[0.06] rounded px-1.5 py-0.5"
                        title={c.description || ''}
                      >
                        <b className="text-white">{c.indicator || '—'}</b>
                        <span className="text-slate-500">@ {c.timeframe}</span>
                        <span className={`text-[9px] ${src.color}`}>({src.text})</span>
                      </span>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Risk plan ─────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 gap-2 text-[10px]">
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Stop loss</div>
          <div className="text-white font-medium">{tpl.risk?.stop_loss_type || '—'}</div>
        </div>
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Take profit</div>
          <div className="text-white font-medium">{tpl.risk?.take_profit_type || '—'}</div>
        </div>
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Risk / Reward</div>
          <div className="text-white font-medium">{tpl.risk?.risk_reward?.toFixed(2) || '—'}R</div>
        </div>
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Risk per trade</div>
          <div className="text-white font-medium">{tpl.risk?.risk_per_trade_pct?.toFixed(2) || '—'}%</div>
        </div>
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Max trades / day</div>
          <div className="text-white font-medium">{tpl.trade_limits?.max_trades_per_day ?? '—'}</div>
        </div>
        <div className="bg-[#131722]/50 border border-white/[0.04] rounded px-2 py-1.5">
          <div className="text-slate-500">Cooldown (candles)</div>
          <div className="text-white font-medium">{tpl.trade_limits?.cooldown_candles ?? '—'}</div>
        </div>
      </div>

      {/* ── Missing / inferred / conflicts ───────────────────────────── */}
      {(tpl.missing_fields.length > 0 || tpl.inferred_fields.length > 0 || tpl.conflicts.length > 0) && (
        <div className="space-y-1">
          {tpl.missing_fields.length > 0 && (
            <div className="text-[10px] text-red-300">
              ⚠️ Missing critical: <span className="font-mono">{tpl.missing_fields.join(', ')}</span>
            </div>
          )}
          {tpl.inferred_fields.length > 0 && (
            <div className="text-[10px] text-amber-300">
              🔧 Filled by defaults: <span className="font-mono">{tpl.inferred_fields.join(', ')}</span>
            </div>
          )}
          {tpl.conflicts.length > 0 && (
            <div className="text-[10px] text-red-300 space-y-0.5">
              {tpl.conflicts.map((c, i) => <div key={i}>⚠️ {c}</div>)}
            </div>
          )}
        </div>
      )}

      {/* ── Resolver notes (audit trail) ─────────────────────────────── */}
      {tpl.resolver_notes.length > 0 && (
        <details className="text-[10px]">
          <summary className="text-slate-400 cursor-pointer hover:text-slate-300">
            Why this score? ({tpl.resolver_notes.length} notes)
          </summary>
          <ul className="mt-1 pl-3 space-y-0.5 text-slate-500">
            {tpl.resolver_notes.map((n, i) => <li key={i}>• {n}</li>)}
          </ul>
        </details>
      )}
    </div>
  );
}
