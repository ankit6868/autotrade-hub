'use client';

import { useState, useEffect } from 'react';
import { api } from '@/lib/api';

/**
 * Add / Reduce Margin modal — mirrors KuCoin's panel.
 *
 * Triggered from any open-position row's "Edit" / margin icon. Paper
 * positions write to the engine directly; live positions call KuCoin's
 * /api/v1/position/margin-deposit through our backend.
 *
 * Liquidation-price preview is computed locally from the position's
 * entry price + leverage so the user can see the impact BEFORE clicking
 * Confirm. The backend recomputes authoritatively after the action.
 */

type Position = {
  pair: string;
  direction: 'long' | 'short';
  entry: number;
  size: number;           // current margin (USDT)
  leverage: number;
  liquidation_price?: number;
  // Optional unique identifier so add/reduce-margin targets the exact
  // row even when hedge mode has long + short on the same pair.
  position_id?: string;
};

type Props = {
  position: Position;
  mode: 'paper' | 'live';
  walletAvailable: number;   // for the "max addable" hint
  onClose: () => void;
  onSuccess: () => void;
};

export default function MarginAdjustModal({
  position, mode, walletAvailable, onClose, onSuccess,
}: Props) {
  const [tab, setTab] = useState<'add' | 'reduce'>('add');
  const [amount, setAmount] = useState<string>('');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [autoDeposit, setAutoDeposit] = useState(false);

  const amt = Number(amount);
  const isValid = !isNaN(amt) && amt >= 0.01 && amt <= 1_000_000;
  const maxAddable = walletAvailable;
  // Match backend's safety floor exactly (position_reduce_margin):
  //   min_margin = max(0.5, size × 0.10)
  // Reducible = size - min_margin.
  // Old code did size − size×0.10 − 0.5 (the floor terms stacked) which
  // showed a too-pessimistic max and could even go negative for small
  // positions, displaying "0.00" even when ~0.7 USDT was reducible.
  const minMargin = Math.max(0.5, position.size * 0.10);
  const maxReducible = Math.max(0, position.size - minMargin);

  // Preview impact (matches backend's _recompute_liq_price math).
  // notional = original margin × original leverage. After margin change
  // the notional is unchanged (margin add/reduce doesn't change position
  // size); only the EFFECTIVE leverage changes:
  //   effective_lev = notional / new_margin
  const previewNewMargin = tab === 'add'
    ? position.size + (isValid ? amt : 0)
    : position.size - (isValid ? amt : 0);
  const notional = position.size * position.leverage;
  const previewEffectiveLev = previewNewMargin > 0.01
    ? notional / previewNewMargin
    : position.leverage;
  // Liq is unreachable in normal market when effective leverage drops
  // below ~1 (position is over-collateralised). Show "Never" instead of
  // a confusing negative number that the old code clamped to 0.
  const liqUnreachable = isValid && previewEffectiveLev < 1.001;

  const previewLiqPrice = (() => {
    if (!isValid) return position.liquidation_price ?? 0;
    if (liqUnreachable) return null;
    if (position.direction === 'long') {
      return position.entry * (1 - 1 / previewEffectiveLev);
    } else {
      return position.entry * (1 + 1 / previewEffectiveLev);
    }
  })();

  // Reset on tab switch
  useEffect(() => { setAmount(''); setErr(null); }, [tab]);

  async function submit() {
    if (!isValid) { setErr('Enter a valid amount (0.01–1,000,000 USDT)'); return; }
    setBusy(true); setErr(null);
    try {
      const res = tab === 'add'
        ? await api.futures.addMargin({
            pair: position.pair, mode, amount: amt,
            direction: position.direction,
            ...(position.position_id ? { position_id: position.position_id } : {}),
          })
        : await api.futures.reduceMargin({
            pair: position.pair, mode: 'paper', amount: amt,
            direction: position.direction,
            ...(position.position_id ? { position_id: position.position_id } : {}),
          });
      if (res?.error) {
        setErr(res.error);
        setBusy(false);
        return;
      }
      onSuccess();
      onClose();
    } catch (e: any) {
      setErr(e?.message || String(e));
      setBusy(false);
    }
  }

  const fmtPrice = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: 2 });

  return (
    <div className="fixed inset-0 z-50 flex items-end sm:items-center justify-center bg-black/70 backdrop-blur-sm p-3 sm:p-6"
         onClick={onClose}>
      <div className="bg-[#0d1424] border border-white/[0.08] rounded-2xl w-full max-w-md overflow-hidden"
           onClick={e => e.stopPropagation()}>
        {/* Tabs */}
        <div className="flex border-b border-white/[0.06]">
          <button
            onClick={() => setTab('add')}
            className={`flex-1 py-3 text-sm font-semibold transition-colors ${
              tab === 'add' ? 'text-emerald-300 border-b-2 border-emerald-400' : 'text-slate-400'
            }`}
          >
            Add Margin
          </button>
          <button
            onClick={() => setTab('reduce')}
            disabled={mode === 'live'}
            className={`flex-1 py-3 text-sm font-semibold transition-colors ${
              tab === 'reduce'
                ? 'text-amber-300 border-b-2 border-amber-400'
                : 'text-slate-400 disabled:opacity-40 disabled:cursor-not-allowed'
            }`}
            title={mode === 'live' ? 'Live reduce-margin not supported — use partial close instead' : ''}
          >
            Reduce Margin
          </button>
          <button onClick={onClose} className="px-4 text-slate-400 hover:text-white text-xl">×</button>
        </div>

        <div className="p-5">
          {/* Position summary */}
          <div className="flex items-center justify-between mb-4">
            <div className="flex items-center gap-2">
              <span className="text-base font-bold text-white">{position.pair} Perp</span>
              <span className={`px-2 py-0.5 rounded text-[10px] font-bold ${
                position.direction === 'short'
                  ? 'bg-red-500/15 text-red-300'
                  : 'bg-emerald-500/15 text-emerald-300'
              }`}>
                {position.direction === 'long' ? 'Long' : 'Short'}
              </span>
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-700/60 text-slate-300">
                {mode.toUpperCase()}
              </span>
            </div>
          </div>

          {/* Amount input + slider */}
          <div className="mb-4">
            <label className="text-xs text-slate-400 mb-1.5 block">
              {tab === 'add' ? 'Add Margin' : 'Reduce Margin'}
            </label>
            <div className="relative">
              <input
                type="number"
                inputMode="decimal"
                step="0.01"
                value={amount}
                onChange={e => { setAmount(e.target.value); setErr(null); }}
                placeholder="0.01"
                className="w-full bg-[#1a2236] border border-white/[0.06] rounded-lg px-3 py-2.5 text-base text-white placeholder:text-slate-600 focus:outline-none focus:border-sky-500/50"
              />
              <span className="absolute right-3 top-1/2 -translate-y-1/2 text-xs text-slate-500">USDT</span>
            </div>
            {/* Quick percentage chips */}
            <div className="flex gap-1.5 mt-2">
              {[0.25, 0.50, 0.75, 1.00].map(pct => (
                <button
                  key={pct}
                  type="button"
                  onClick={() => setAmount(
                    String(((tab === 'add' ? maxAddable : maxReducible) * pct).toFixed(2))
                  )}
                  className="flex-1 text-[10px] py-1 rounded bg-slate-700/40 text-slate-300 hover:bg-slate-700"
                >
                  {Math.round(pct * 100)}%
                </button>
              ))}
            </div>
            <div className="flex justify-between mt-2 text-[10px] text-slate-500">
              <span>Max {tab === 'add' ? 'addable' : 'reducible'}</span>
              <span className="text-slate-300 font-medium">
                {(tab === 'add' ? maxAddable : maxReducible).toFixed(2)} USDT
              </span>
            </div>
            {mode === 'live' && tab === 'add' && (
              <label className="flex items-center gap-2 mt-3 cursor-pointer">
                <input type="checkbox" checked={autoDeposit} onChange={e => setAutoDeposit(e.target.checked)}
                       className="accent-sky-500" />
                <span className="text-xs text-slate-400">Auto-deposit margin from spot wallet on liq threat</span>
              </label>
            )}
          </div>

          {/* Before / After table */}
          <div className="bg-[#0a1020] rounded-lg p-3 mb-4 border border-white/[0.04]">
            {/* Tiny explainer — users were thinking "Leverage" was the
                position-leverage setting being changed (it can't be).
                The number shown is EFFECTIVE leverage, computed from
                notional/margin. Position leverage itself stays at the
                value the position was opened with. */}
            <p className="text-[9px] text-slate-500 mb-2 leading-snug">
              Position leverage is <span className="text-slate-300 font-medium">locked at {position.leverage.toFixed(0)}×</span> —
              exchanges don't allow changing it on an open trade. The
              "Effective Lev." row below is notional ÷ margin: it shrinks
              when you add margin (your P&L volatility per tick goes down).
            </p>
            <div className="grid grid-cols-3 gap-2 text-[11px]">
              <div className="text-slate-500"></div>
              <div className="text-right text-slate-400 font-medium">Current</div>
              <div className="text-right text-slate-400 font-medium">After</div>
              <div className="text-slate-400">Margin</div>
              <div className="text-right text-slate-200 tabular-nums">{position.size.toFixed(2)}</div>
              <div className={`text-right tabular-nums ${
                isValid ? (tab === 'add' ? 'text-emerald-300' : 'text-amber-300') : 'text-slate-600'
              }`}>
                {isValid
                  ? (tab === 'add' ? position.size + amt : position.size - amt).toFixed(2)
                  : '—'}
              </div>
              <div
                className="text-slate-400 flex items-center gap-1"
                title={
                  "Position leverage is LOCKED at " + position.leverage.toFixed(0) + "x for the whole life " +
                  "of this trade — exchanges (KuCoin included) don't let you change it on an open position. " +
                  "The number shown here is EFFECTIVE leverage: notional ÷ margin. Adding margin to the " +
                  "same notional drops it, which is the whole point — your P&L volatility per price tick " +
                  "shrinks. The position itself is still a " + position.leverage.toFixed(0) + "x position."
                }
              >
                Effective Lev.
                <span className="text-[9px] px-1 py-0.5 rounded bg-slate-700/60 text-slate-400" title="Position leverage is locked at entry">🔒</span>
              </div>
              <div className="text-right text-slate-200 tabular-nums">{position.leverage.toFixed(2)}x</div>
              <div className={`text-right tabular-nums ${isValid ? 'text-sky-300' : 'text-slate-600'}`}>
                {isValid ? previewEffectiveLev.toFixed(2) + 'x' : '—'}
              </div>
              <div className="text-slate-400">Liq. Price</div>
              <div className="text-right text-amber-300 tabular-nums">
                {fmtPrice(position.liquidation_price || 0)}
              </div>
              <div className={`text-right tabular-nums ${
                isValid ? (tab === 'add' ? 'text-emerald-300' : 'text-red-300') : 'text-slate-600'
              }`}
              title={liqUnreachable
                ? "Adding this much margin drops effective leverage below 1× — position is over-collateralised and can't liquidate from a typical price move."
                : ''}
              >
                {isValid
                  ? (liqUnreachable || previewLiqPrice === null
                      ? 'Never'
                      : fmtPrice(previewLiqPrice as number))
                  : '—'}
              </div>
              <div className="text-slate-400">Entry Price</div>
              <div className="text-right text-slate-300 tabular-nums">{fmtPrice(position.entry)}</div>
              <div className="text-right text-slate-300 tabular-nums">{fmtPrice(position.entry)}</div>
            </div>
          </div>

          {/* Help text */}
          <p className="text-[10px] text-slate-500 leading-relaxed mb-4">
            {tab === 'add'
              ? 'Adding margin lowers your effective leverage and moves the liquidation price further from entry — reducing liq risk.'
              : 'Reducing margin raises your effective leverage and brings the liquidation price CLOSER to entry — increasing liq risk. Safety floor: 10% of current margin.'}
          </p>

          {err && (
            <div className="mb-3 p-2 rounded-md bg-red-500/10 border border-red-500/30 text-[11px] text-red-300">
              {err}
            </div>
          )}

          {/* Confirm */}
          <button
            disabled={busy || !isValid}
            onClick={submit}
            className={`w-full py-3 rounded-xl text-sm font-semibold transition-colors ${
              busy || !isValid
                ? 'bg-slate-700/40 text-slate-500 cursor-not-allowed'
                : tab === 'add'
                  ? 'bg-emerald-500/80 text-white hover:bg-emerald-500'
                  : 'bg-amber-500/80 text-white hover:bg-amber-500'
            }`}
          >
            {busy ? 'Submitting…' : (tab === 'add' ? 'Confirm Add' : 'Confirm Reduce')}
          </button>
        </div>
      </div>
    </div>
  );
}
