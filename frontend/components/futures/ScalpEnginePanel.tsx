'use client';

/**
 * ScalpEnginePanel — UI for the WebSocket-driven paper scalp engine.
 *
 * Sits at the top of the futures-paper page as a self-contained card.
 * Doesn't touch the existing futures paper trading state; runs in
 * parallel via the /api/paper-scalp/* endpoints. One engine per
 * (pair, timeframe) — user can run multiple engines simultaneously.
 *
 * Polls /status every 3 seconds when an engine is running so the
 * positions/PNL panel stays fresh without hammering the backend.
 */
import { useEffect, useState, useCallback } from 'react';

type StatusSnapshot = {
  status: string;
  pair: string;
  symbol: string;
  timeframe: string;
  leverage: number;
  margin_pct: number;
  vip_tier: number;
  fee_rates_pct: { maker: number; taker: number };
  maker_only_entry: boolean;
  arm_enabled: boolean;
  starting_balance: number;
  balance: number;
  realised_pnl: number;
  unrealised_pnl: number;
  started_at_ts: number;
  last_tick_ts: number;
  last_bar_close_ts: number;
  bars_in_history: number;
  ticks_received: number;
  signals_fired: number;
  signals_skipped_no_fill: number;
  positions_opened: number;
  positions_closed: number;
  open_positions: number;
  open_positions_detail: any[];
  recent_closed_trades: any[];
  error_message?: string;
};

type Strategy = { id: number; name: string };

export default function ScalpEnginePanel({ strategies }: { strategies: Strategy[] }) {
  // Form state
  const [strategyId,      setStrategyId]      = useState<number | ''>('');
  const [pair,            setPair]            = useState('BTC/USDT');
  const [timeframe,       setTimeframe]       = useState('1m');
  const [startingBalance, setStartingBalance] = useState(1000);
  const [leverage,        setLeverage]        = useState(10);
  const [marginPct,       setMarginPct]       = useState(5);
  const [vipTier,         setVipTier]         = useState(0);
  const [makerOnly,       setMakerOnly]       = useState(false);
  const [armEnabled,      setArmEnabled]      = useState(false);
  const [armTp1ClosePct,  setArmTp1ClosePct]  = useState(50);
  const [armBeMode,       setArmBeMode]       = useState<'leverage' | 'manual_pct' | 'entry'>('leverage');

  // Runtime state
  const [engines,   setEngines]   = useState<StatusSnapshot[]>([]);
  const [busy,      setBusy]      = useState(false);
  const [errorMsg,  setErrorMsg]  = useState('');
  const [expanded,  setExpanded]  = useState(false);  // collapsed by default to not crowd the page

  // Pre-select first strategy when strategies load
  useEffect(() => {
    if (strategies.length > 0 && strategyId === '') {
      setStrategyId(strategies[0].id);
    }
  }, [strategies, strategyId]);

  // Poll /status every 3 seconds when at least one engine is shown
  const refresh = useCallback(async () => {
    try {
      const resp = await fetch('/api/paper-scalp/status', { credentials: 'include' });
      if (!resp.ok) return;
      const data = await resp.json();
      setEngines(data.engines || []);
    } catch {
      // network blip — silently retry next poll
    }
  }, []);

  useEffect(() => {
    refresh();
    const tick = setInterval(refresh, 3000);
    return () => clearInterval(tick);
  }, [refresh]);

  const startEngine = async () => {
    if (!strategyId) { setErrorMsg('Pick a strategy first'); return; }
    setBusy(true); setErrorMsg('');
    try {
      const resp = await fetch('/api/paper-scalp/start', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_id:       strategyId,
          pair, timeframe,
          starting_balance:  startingBalance,
          leverage, margin_pct: marginPct,
          vip_tier:          vipTier,
          maker_only_entry:  makerOnly,
          arm_enabled:       armEnabled,
          arm_tp1_close_pct: armTp1ClosePct,
          arm_be_mode:       armBeMode,
          arm_be_buffer_pct: 1.0,
          arm_trail_to_tp1:  true,
          sltp_source:       'strategy',
        }),
      });
      const data = await resp.json();
      if (!data.ok) {
        setErrorMsg(data.error || 'start failed');
      } else {
        await refresh();
      }
    } catch (e: any) {
      setErrorMsg(e?.message || 'network error');
    } finally {
      setBusy(false);
    }
  };

  const stopEngine = async (p: string, tf: string) => {
    setBusy(true); setErrorMsg('');
    try {
      const resp = await fetch('/api/paper-scalp/stop', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pair: p, timeframe: tf }),
      });
      const data = await resp.json();
      if (!data.ok) setErrorMsg(data.error || 'stop failed');
      await refresh();
    } catch (e: any) {
      setErrorMsg(e?.message || 'network error');
    } finally {
      setBusy(false);
    }
  };

  const statusColor = (s: string) =>
    s === 'active'     ? 'text-emerald-300 bg-emerald-500/10 border-emerald-500/30' :
    s === 'warming_up' ? 'text-amber-300 bg-amber-500/10 border-amber-500/30' :
    s === 'connecting' ? 'text-sky-300 bg-sky-500/10 border-sky-500/30' :
    s === 'error'      ? 'text-red-300 bg-red-500/10 border-red-500/30' :
                         'text-slate-400 bg-slate-500/10 border-slate-500/30';

  return (
    <div className="mb-4 rounded-xl border border-purple-500/30 bg-purple-500/5">
      {/* Header — always visible */}
      <button
        type="button"
        onClick={() => setExpanded(!expanded)}
        className="w-full p-3 flex items-center justify-between text-left"
      >
        <div className="flex items-center gap-3">
          <span className="text-lg">⚡</span>
          <div>
            <div className="text-sm font-semibold text-purple-200">
              Scalp Engine (WebSocket, real-time)
            </div>
            <div className="text-[11px] text-slate-400">
              Purpose-built for 1m scalping — uses live WS ticks, evaluates strategy on every bar close,
              per-tick SL/TP detection. {engines.length > 0 && <span className="text-emerald-300">{engines.length} running</span>}
            </div>
          </div>
        </div>
        <span className="text-xs text-slate-400">{expanded ? '▲ collapse' : '▼ expand'}</span>
      </button>

      {expanded && (
        <div className="px-3 pb-3 border-t border-purple-500/20">
          {/* Active engines */}
          {engines.length > 0 && (
            <div className="mt-3 space-y-2">
              {engines.map((e, i) => (
                <div key={`${e.pair}-${e.timeframe}-${i}`}
                     className="rounded-lg border border-[#2a3a52] bg-[#1a2236] p-3">
                  <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-mono text-sm font-semibold text-slate-200">
                        {e.pair} {e.timeframe}
                      </span>
                      <span className={`chip text-[10px] border ${statusColor(e.status)}`}>
                        {e.status.toUpperCase()}
                      </span>
                      <span className="chip text-[10px] bg-blue-500/10 border-blue-500/30 text-blue-300">
                        {e.leverage}x · {e.margin_pct}%/trade
                      </span>
                      <span className="chip text-[10px] bg-purple-500/10 border-purple-500/30 text-purple-300">
                        VIP{e.vip_tier} {e.maker_only_entry ? '· maker-only' : '· taker'}
                      </span>
                      {e.arm_enabled && (
                        <span className="chip text-[10px] bg-amber-500/10 border-amber-500/30 text-amber-300">
                          ARM on
                        </span>
                      )}
                    </div>
                    <button
                      type="button"
                      onClick={() => stopEngine(e.pair, e.timeframe)}
                      disabled={busy}
                      className="btn-secondary text-xs !py-1 !px-3"
                    >
                      ⏹ Stop
                    </button>
                  </div>

                  {/* Stats grid */}
                  <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
                    <Stat label="Balance" value={`$${e.balance.toFixed(2)}`}
                          sub={`start $${e.starting_balance.toFixed(0)}`} />
                    <Stat label="Realised P&L"
                          value={`${e.realised_pnl >= 0 ? '+' : ''}${e.realised_pnl.toFixed(2)}`}
                          sub="USDT"
                          color={e.realised_pnl >= 0 ? 'text-emerald-300' : 'text-red-300'} />
                    <Stat label="Unrealised P&L"
                          value={`${e.unrealised_pnl >= 0 ? '+' : ''}${e.unrealised_pnl.toFixed(2)}`}
                          sub={`${e.open_positions} open`}
                          color={e.unrealised_pnl >= 0 ? 'text-emerald-300' : 'text-red-300'} />
                    <Stat label="Activity"
                          value={`${e.signals_fired}/${e.positions_opened}/${e.positions_closed}`}
                          sub="signals / opened / closed" />
                    <Stat label="Ticks received" value={e.ticks_received.toLocaleString()}
                          sub={e.last_tick_ts ? `${Math.round(Date.now()/1000 - e.last_tick_ts)}s ago` : 'no ticks yet'} />
                    <Stat label="Bars in history" value={String(e.bars_in_history)}
                          sub={e.last_bar_close_ts ? `last close ${new Date(e.last_bar_close_ts*1000).toLocaleTimeString()}` : ''} />
                    <Stat label="Maker skipped"
                          value={String(e.signals_skipped_no_fill)}
                          sub="non-fills" />
                    <Stat label="Fees"
                          value={`m ${e.fee_rates_pct.maker}% / t ${e.fee_rates_pct.taker}%`}
                          sub={`VIP${e.vip_tier}`} />
                  </div>

                  {/* Open positions */}
                  {e.open_positions_detail.length > 0 && (
                    <div className="mt-3 text-[11px]">
                      <div className="text-slate-400 mb-1">Open positions:</div>
                      <div className="space-y-1">
                        {e.open_positions_detail.map((p, j) => (
                          <div key={j} className="font-mono text-slate-300 bg-[#0e1525] rounded px-2 py-1">
                            <span className={p.direction === 'long' ? 'text-emerald-300' : 'text-red-300'}>
                              {p.direction.toUpperCase()}
                            </span>
                            {' @ '}{p.entry_price}
                            {' · SL '}<span className="text-red-300/70">{p.sl_price}</span>
                            {' · TP '}<span className="text-emerald-300/70">{p.tp_price}</span>
                            {p.tp2_price && <> {' · TP2 '}<span className="text-emerald-300/50">{p.tp2_price}</span></>}
                            {' · margin $'}{p.margin}
                            {p.tp1_hit && <span className="ml-2 text-amber-300">[TP1 hit]</span>}
                            {p.entry_was_maker && <span className="ml-2 text-purple-300">[maker]</span>}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {/* Recent closed trades */}
                  {e.recent_closed_trades.length > 0 && (
                    <div className="mt-3 text-[11px]">
                      <div className="text-slate-400 mb-1">
                        Recent closed ({e.recent_closed_trades.length}):
                      </div>
                      <div className="space-y-1 max-h-32 overflow-y-auto">
                        {e.recent_closed_trades.slice().reverse().slice(0, 10).map((t, j) => (
                          <div key={j} className="font-mono text-slate-400 bg-[#0e1525] rounded px-2 py-1">
                            <span className={t.direction === 'long' ? 'text-emerald-300/80' : 'text-red-300/80'}>
                              {t.direction[0].toUpperCase()}
                            </span>
                            {' '}{t.entry_price} → {t.exit_price}
                            {' · '}
                            <span className={t.profit_abs >= 0 ? 'text-emerald-300' : 'text-red-300'}>
                              {t.profit_abs >= 0 ? '+' : ''}{t.profit_abs}
                            </span>
                            {' ('}{t.profit_pct >= 0 ? '+' : ''}{t.profit_pct}%{')'}
                            {' · '}<span className="text-slate-500">{t.exit_reason}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  {e.error_message && (
                    <div className="mt-2 text-xs text-red-300">⚠️ {e.error_message}</div>
                  )}
                </div>
              ))}
            </div>
          )}

          {/* New engine form */}
          <div className="mt-3 rounded-lg border border-dashed border-purple-500/30 p-3">
            <div className="text-xs font-semibold text-purple-200 mb-2">+ Start a new scalp engine</div>
            <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
              <div>
                <label className="label text-[11px]">Strategy</label>
                <select
                  value={strategyId}
                  onChange={e => setStrategyId(Number(e.target.value))}
                  className="input !py-1 !text-xs w-full"
                >
                  <option value="">— pick one —</option>
                  {strategies.map(s => (
                    <option key={s.id} value={s.id}>{s.name}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label text-[11px]">Pair</label>
                <input value={pair} onChange={e => setPair(e.target.value.toUpperCase())}
                       className="input !py-1 !text-xs w-full" placeholder="BTC/USDT" />
              </div>
              <div>
                <label className="label text-[11px]">Timeframe</label>
                <select value={timeframe} onChange={e => setTimeframe(e.target.value)}
                        className="input !py-1 !text-xs w-full">
                  {['1m','3m','5m','15m','30m','1h','4h'].map(tf => (
                    <option key={tf} value={tf}>{tf}</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label text-[11px]">Starting balance (USDT)</label>
                <input type="number" min={10} value={startingBalance}
                       onChange={e => setStartingBalance(Number(e.target.value))}
                       className="input !py-1 !text-xs w-full" />
              </div>
              <div>
                <label className="label text-[11px]">Leverage</label>
                <input type="number" min={1} max={125} value={leverage}
                       onChange={e => setLeverage(Number(e.target.value))}
                       className="input !py-1 !text-xs w-full" />
              </div>
              <div>
                <label className="label text-[11px]">Margin/trade %</label>
                <input type="number" step={0.5} min={0.1} max={100} value={marginPct}
                       onChange={e => setMarginPct(Number(e.target.value))}
                       className="input !py-1 !text-xs w-full" />
              </div>
              <div>
                <label className="label text-[11px]">VIP tier</label>
                <select value={vipTier} onChange={e => setVipTier(Number(e.target.value))}
                        className="input !py-1 !text-xs w-full">
                  {Array.from({length: 13}, (_, i) => (
                    <option key={i} value={i}>VIP{i}</option>
                  ))}
                </select>
              </div>
              <div className="flex items-center gap-2">
                <input type="checkbox" id="scalp-maker" checked={makerOnly}
                       onChange={e => setMakerOnly(e.target.checked)}
                       className="accent-emerald-500" />
                <label htmlFor="scalp-maker" className="text-[11px] text-slate-300 cursor-pointer">
                  Maker-only entries
                </label>
              </div>
              <div className="flex items-center gap-2">
                <input type="checkbox" id="scalp-arm" checked={armEnabled}
                       onChange={e => setArmEnabled(e.target.checked)}
                       className="accent-purple-500" />
                <label htmlFor="scalp-arm" className="text-[11px] text-slate-300 cursor-pointer">
                  ARM (TP1/TP2 split + BE trail)
                </label>
              </div>
              {armEnabled && (
                <>
                  <div>
                    <label className="label text-[11px]">TP1 close %</label>
                    <input type="number" min={1} max={99} value={armTp1ClosePct}
                           onChange={e => setArmTp1ClosePct(Number(e.target.value))}
                           className="input !py-1 !text-xs w-full" />
                  </div>
                  <div>
                    <label className="label text-[11px]">BE mode</label>
                    <select value={armBeMode}
                            onChange={e => setArmBeMode(e.target.value as any)}
                            className="input !py-1 !text-xs w-full">
                      <option value="leverage">Leverage-auto</option>
                      <option value="manual_pct">Manual %</option>
                      <option value="entry">At entry</option>
                    </select>
                  </div>
                </>
              )}
            </div>

            <div className="mt-3 flex items-center gap-3 flex-wrap">
              <button
                type="button"
                onClick={startEngine}
                disabled={busy || !strategyId}
                className="btn-primary text-xs !py-1 !px-4 bg-purple-600 hover:bg-purple-700 border-purple-500"
              >
                {busy ? '...' : '▶ Start scalp engine'}
              </button>
              {errorMsg && <span className="text-xs text-red-300">{errorMsg}</span>}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-[#0e1525] rounded px-2 py-1.5">
      <div className="text-[10px] text-slate-500 uppercase tracking-wider">{label}</div>
      <div className={`text-sm font-mono font-semibold ${color || 'text-slate-200'}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
    </div>
  );
}
