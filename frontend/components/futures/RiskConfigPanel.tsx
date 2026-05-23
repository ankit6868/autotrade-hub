'use client';
import { useEffect, useState } from 'react';
import { api } from '@/lib/api';

/**
 * NICE-4 (FR-04) — Per-TF risk config panel.
 *
 * Lets the user override TIMEFRAME_CONFIG defaults (sl_mult, tp_mult,
 * min_rr, atr_period) per timeframe. Stored on Config.risk_config_json
 * and applied by risk_engine.compute_tp_sl in both backtest + live.
 *
 * The defaults are shown next to the override input so the user always
 * sees the baseline they're nudging. Empty input = revert to default.
 */
interface TFRow {
  atr_period: number;
  sl_mult:    number;
  tp_mult:    number;
  min_rr:     number;
  style?:     string;
}

const FIELDS: Array<{ key: keyof TFRow; label: string; step: number; min: number; max: number }> = [
  { key: 'atr_period', label: 'ATR period', step: 1,    min: 5,   max: 50  },
  { key: 'sl_mult',    label: 'SL × ATR',    step: 0.05, min: 0.1, max: 10  },
  { key: 'tp_mult',    label: 'TP × ATR',    step: 0.05, min: 0.1, max: 10  },
  { key: 'min_rr',     label: 'Min RR',      step: 0.1,  min: 1,   max: 5   },
];

export default function RiskConfigPanel() {
  const [defaults,  setDefaults]  = useState<Record<string, TFRow>>({});
  const [overrides, setOverrides] = useState<Record<string, Partial<TFRow>>>({});
  const [loading,   setLoading]   = useState(true);
  const [saving,    setSaving]    = useState(false);
  const [saved,     setSaved]     = useState(false);
  const [err,       setErr]       = useState<string | null>(null);

  useEffect(() => {
    api.futures.riskConfig.get()
      .then((d: any) => {
        setDefaults(d?.defaults  || {});
        setOverrides(d?.overrides || {});
      })
      .catch((e: any) => setErr(String(e?.message || e)))
      .finally(() => setLoading(false));
  }, []);

  function setVal(tf: string, key: keyof TFRow, raw: string) {
    setOverrides(prev => {
      const next = { ...prev };
      const cur  = { ...(next[tf] || {}) } as Partial<TFRow>;
      if (raw === '' || raw === null) {
        delete (cur as any)[key];
      } else {
        const n = Number(raw);
        if (Number.isFinite(n)) (cur as any)[key] = n;
      }
      if (Object.keys(cur).length === 0) {
        delete next[tf];
      } else {
        next[tf] = cur;
      }
      return next;
    });
    setSaved(false);
  }

  async function save() {
    setSaving(true); setErr(null); setSaved(false);
    try {
      const r = await api.futures.riskConfig.put(overrides);
      if (r?.error) setErr(r.error);
      else setSaved(true);
    } catch (e: any) {
      setErr(String(e?.message || e));
    }
    setSaving(false);
  }

  if (loading) return <div className="text-xs text-slate-500 p-3">Loading risk config…</div>;

  const tfs = Object.keys(defaults);

  return (
    <div className="space-y-3">
      <div>
        <h3 className="text-sm font-bold text-white">📐 Per-Timeframe Risk Config</h3>
        <p className="text-[10px] text-slate-400 leading-snug mt-0.5">
          Override the default ATR multipliers per timeframe. Empty fields use the default.
          Changes apply immediately to new signals in the backtester AND live bots.
        </p>
      </div>

      {err && <p className="text-red-400 text-xs">{err}</p>}
      {saved && <p className="text-emerald-400 text-xs">✓ Saved — overrides will apply on the next signal scan.</p>}

      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-[10px] border-b border-white/[0.06]">
              <th className="text-left  px-2 py-1.5">TF</th>
              <th className="text-left  px-2 py-1.5">Style</th>
              {FIELDS.map(f => (
                <th key={f.key} className="text-right px-2 py-1.5 font-medium" title={`Default vs your override for ${f.label}`}>
                  {f.label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {tfs.map(tf => {
              const def = defaults[tf];
              const ov  = overrides[tf] || {};
              return (
                <tr key={tf} className="border-b border-white/[0.03] hover:bg-white/[0.02]">
                  <td className="px-2 py-1.5 text-white font-bold">{tf}</td>
                  <td className="px-2 py-1.5 text-slate-500">{def?.style || ''}</td>
                  {FIELDS.map(f => {
                    const defVal = def?.[f.key];
                    const ovVal  = (ov as any)[f.key];
                    const effective = ovVal !== undefined ? ovVal : defVal;
                    const isOverridden = ovVal !== undefined;
                    return (
                      <td key={f.key} className="px-2 py-1.5 text-right">
                        <div className="flex items-center justify-end gap-1.5">
                          <span className={`text-[9px] ${isOverridden ? 'text-purple-300' : 'text-slate-600'}`} title={`default: ${defVal}`}>
                            (def {defVal})
                          </span>
                          <input
                            type="number"
                            step={f.step}
                            min={f.min}
                            max={f.max}
                            value={ovVal !== undefined ? ovVal : ''}
                            onChange={e => setVal(tf, f.key, e.target.value)}
                            placeholder={String(defVal ?? '')}
                            className={`w-16 px-1.5 py-0.5 rounded bg-[#1e222d] border text-[11px] text-white outline-none text-right ${
                              isOverridden ? 'border-purple-500/40' : 'border-white/[0.06]'
                            }`}
                          />
                        </div>
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={save}
          disabled={saving}
          className="px-3 py-1.5 rounded bg-emerald-500 text-white text-xs font-bold hover:bg-emerald-400 disabled:opacity-50"
        >
          {saving ? 'Saving…' : 'Save overrides'}
        </button>
        <button
          onClick={() => { setOverrides({}); setSaved(false); }}
          className="px-3 py-1.5 rounded border border-white/[0.1] text-slate-300 text-xs hover:bg-white/[0.05]"
        >
          Reset all to defaults
        </button>
      </div>
    </div>
  );
}
