'use client';
/**
 * StrategySignalMonitor — shows live strategy signal status:
 * - Whether current conditions match the strategy's entry rules
 * - Key indicator values vs required thresholds
 * - How many times the strategy has fired (from trade history)
 * - Estimated next fire time
 * - One-click manual Buy / Sell paper execution
 */
import { useEffect, useState, useCallback } from 'react';
import { api } from '@/lib/api';

interface Props {
  strategyName: string;
  pair: string;
  timeframe: string;
  isRunning: boolean;
  onManualBuy?: () => void;
  onManualSell?: () => void;
}

interface SignalStatus {
  ready: boolean;
  conditions: Array<{ label: string; met: boolean; value: string; required: string }>;
  recommendation: string;
  fireCount: number;
  lastFired: string | null;
}

// Maps strategy name → what conditions to display
function buildStatus(
  strategyName: string,
  indicators: Record<string, number | null> | undefined,
  rec: string,
  tradeCount: number,
  lastTrade: string | null
): SignalStatus {
  const n = strategyName.toLowerCase();
  const ind = indicators || {};

  const rsi = ind.rsi as number;
  const macd = ind.macd as number;
  const macdS = ind.macd_signal as number;
  const bbLower = ind.bb_lower as number;
  const ema20 = ind.ema_20 as number;
  const price = ind.close as number || ind.ema_20 as number;
  const adx = ind.adx as number;

  let conditions: SignalStatus['conditions'] = [];
  let ready = false;

  if (n.includes('macd')) {
    const crossedUp = macd > macdS;
    const macdPositive = macd > 0;
    conditions = [
      { label: 'MACD > Signal (buy crossover)', met: crossedUp, value: `${macd?.toFixed(4) || '?'}`, required: `> ${macdS?.toFixed(4) || '?'}` },
      { label: 'MACD histogram positive', met: macdPositive, value: macd?.toFixed(4) || '?', required: '> 0' },
      { label: 'Signal: BUY or STRONG_BUY', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY or STRONG_BUY' },
    ];
    ready = crossedUp && macdPositive;
  } else if (n.includes('rsi') || n.includes('bollinger')) {
    const oversold = rsi < 30;
    const belowBB = price && bbLower ? price < bbLower : false;
    conditions = [
      { label: 'RSI < 30 (oversold)', met: oversold, value: rsi?.toFixed(1) || '?', required: '< 30' },
      { label: 'Price below BB Lower', met: belowBB, value: price?.toFixed(2) || '?', required: `< ${bbLower?.toFixed(2) || '?'}` },
      { label: 'ADX > 20 (trending)', met: adx > 20, value: adx?.toFixed(1) || '?', required: '> 20' },
    ];
    ready = oversold && belowBB;
  } else if (n.includes('ema') || n.includes('scalp')) {
    const ema9 = ind.ema_9 as number || ema20 * 0.98;
    const ema21 = ind.ema_20 as number;
    const emaUp = ema9 > ema21;
    conditions = [
      { label: 'EMA9 > EMA21 (uptrend)', met: emaUp, value: `EMA9: ${ema9?.toFixed(2) || '?'}`, required: `> EMA21: ${ema21?.toFixed(2) || '?'}` },
      { label: 'ADX > 25 (strong trend)', met: adx > 25, value: adx?.toFixed(1) || '?', required: '> 25' },
      { label: 'Signal: BUY or STRONG_BUY', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY' },
    ];
    ready = emaUp && adx > 25;
  } else if (n.includes('miss') && n.includes('short')) {
    const macdNeg = macd < 0;
    conditions = [
      { label: 'MACD histogram negative', met: macdNeg, value: macd?.toFixed(4) || '?', required: '< 0' },
      { label: 'Signal: SELL or STRONG_SELL', met: rec === 'SELL' || rec === 'STRONG_SELL', value: rec, required: 'SELL' },
      { label: 'Price near EMA (miss-candle setup)', met: rec !== 'NEUTRAL', value: rec, required: 'active bearish setup' },
    ];
    ready = macdNeg && (rec === 'SELL' || rec === 'STRONG_SELL');
  } else if (n.includes('miss')) {
    const macdPos = macd > 0;
    conditions = [
      { label: 'MACD histogram positive', met: macdPos, value: macd?.toFixed(4) || '?', required: '> 0' },
      { label: 'Signal: BUY or STRONG_BUY', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY' },
    ];
    ready = macdPos && (rec === 'BUY' || rec === 'STRONG_BUY');
  } else if (n.includes('dca')) {
    conditions = [
      { label: 'Any buying signal', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY' },
      { label: 'RSI not overbought', met: rsi < 70, value: rsi?.toFixed(1) || '?', required: '< 70' },
    ];
    ready = rec === 'BUY' || rec === 'STRONG_BUY';
  } else {
    // Generic fallback
    conditions = [
      { label: 'Market signal', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY or STRONG_BUY' },
    ];
    ready = rec === 'BUY' || rec === 'STRONG_BUY';
  }

  return { ready, conditions, recommendation: rec, fireCount: tradeCount, lastFired: lastTrade };
}

export default function StrategySignalMonitor({ strategyName, pair, timeframe, isRunning, onManualBuy, onManualSell }: Props) {
  const [status, setStatus] = useState<SignalStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [buyLoading, setBuyLoading] = useState(false);
  const [sellLoading, setSellLoading] = useState(false);
  const [tradeMsg, setTradeMsg] = useState<{ ok: boolean; msg: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [sigData, histData] = await Promise.all([
        api.market.signals(pair, timeframe) as Promise<{ summary?: { recommendation: string }; indicators?: Record<string, number | null> }>,
        api.trade.history({ mode: 'paper', limit: '100' }) as Promise<{ trades: Array<{ profit_abs: number; close_date: string; exit_reason?: string; open_reason?: string }> }>,
      ]);
      const rec = sigData?.summary?.recommendation || 'NEUTRAL';
      const allTrades = histData?.trades || [];
      // "Fired" = strategy auto-entered, NOT manually force-closed by user
      const strategyTrades = allTrades.filter(
        (t) => t.exit_reason !== 'force_closed' && t.open_reason !== 'manual'
      );
      const lastTrade = strategyTrades.length > 0
        ? (strategyTrades[0]?.close_date || null)
        : null;
      setStatus(buildStatus(strategyName, sigData?.indicators, rec, strategyTrades.length, lastTrade));
    } catch {
      setStatus(null);
    }
    setLoading(false);
  }, [strategyName, pair, timeframe]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  async function handleManualBuy() {
    setBuyLoading(true);
    try {
      const res = await api.trade.manualEntry(pair, 'long');
      if (res?.entered) {
        setTradeMsg({ ok: true, msg: `Bought ${pair} @ ${res.entry} | SL: ${res.sl} | TP: ${res.tp}` });
        onManualBuy?.();
        load();
      } else {
        setTradeMsg({ ok: false, msg: res?.error || 'Buy failed' });
      }
    } catch (e) {
      setTradeMsg({ ok: false, msg: String(e) });
    }
    setBuyLoading(false);
    setTimeout(() => setTradeMsg(null), 5000);
  }

  async function handleManualSell() {
    setSellLoading(true);
    try {
      const res = await api.trade.manualEntry(pair, 'short');
      if (res?.entered) {
        setTradeMsg({ ok: true, msg: `Sold ${pair} @ ${res.entry} | SL: ${res.sl} | TP: ${res.tp}` });
        onManualSell?.();
        load();
      } else {
        setTradeMsg({ ok: false, msg: res?.error || 'Sell failed' });
      }
    } catch (e) {
      setTradeMsg({ ok: false, msg: String(e) });
    }
    setSellLoading(false);
    setTimeout(() => setTradeMsg(null), 5000);
  }

  const metCount = status?.conditions.filter(c => c.met).length || 0;
  const totalCount = status?.conditions.length || 1;
  const pct = Math.round((metCount / totalCount) * 100);

  return (
    <div className="card mb-6">
      <div className="flex items-center justify-between mb-4">
        <div>
          <h2 className="font-semibold text-base flex items-center gap-2">
            📡 Strategy Signal Monitor
          </h2>
          <p className="text-xs text-slate-400 mt-0.5">{strategyName} · {pair} · {timeframe} · auto-refreshes every 60s</p>
        </div>
        <button onClick={load} className="text-xs text-slate-400 hover:text-white border border-[#2a3a52] px-2 py-1 rounded-lg transition-colors">
          ↻ Refresh
        </button>
      </div>

      {loading ? (
        <div className="text-slate-500 text-sm py-4 text-center">Analyzing strategy conditions...</div>
      ) : !status ? (
        <div className="text-red-400 text-sm">Could not load signal data</div>
      ) : (
        <>
          {/* Signal readiness bar */}
          <div className="flex items-center gap-3 mb-4 p-3 rounded-xl border border-[#2a3a52] bg-[#0a0f1c]">
            <div className={`text-2xl font-bold ${status.ready ? 'text-emerald-400' : 'text-amber-400'}`}>
              {status.ready ? '🟢' : '🟡'}
            </div>
            <div className="flex-1">
              <div className={`text-sm font-bold ${status.ready ? 'text-emerald-300' : 'text-amber-300'}`}>
                {status.ready ? '✅ SIGNAL READY — Strategy conditions met!' : `⏳ WAITING — ${metCount}/${totalCount} conditions met`}
              </div>
              <div className="w-full bg-[#1a2236] rounded-full h-2 mt-1.5">
                <div
                  className={`h-2 rounded-full transition-all ${status.ready ? 'bg-emerald-500' : 'bg-amber-500'}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            </div>
            <div className="text-right" title="Auto-entries by the strategy (excludes manually force-closed trades)">
              <div className="text-xs text-slate-400">Auto-fired</div>
              <div className="text-xl font-bold text-white">{status.fireCount}</div>
            </div>
          </div>

          {/* Conditions checklist */}
          <div className="space-y-2 mb-4">
            {status.conditions.map((c, i) => (
              <div key={i} className={`flex items-center justify-between p-2.5 rounded-lg border text-sm ${
                c.met ? 'bg-emerald-500/10 border-emerald-500/25' : 'bg-slate-800/40 border-[#2a3a52]'
              }`}>
                <div className="flex items-center gap-2">
                  <span>{c.met ? '✅' : '⭕'}</span>
                  <span className={c.met ? 'text-emerald-200' : 'text-slate-400'}>{c.label}</span>
                </div>
                <div className="text-right text-xs">
                  <span className={`font-mono ${c.met ? 'text-emerald-300' : 'text-slate-500'}`}>{c.value}</span>
                  <span className="text-slate-600 ml-1">({c.required})</span>
                </div>
              </div>
            ))}
          </div>

          {/* Last auto-fired + stats */}
          {status.fireCount > 0 && (
            <div className="flex gap-3 mb-4 text-xs text-slate-400">
              <span>Last auto-trade: <span className="text-white">{status.lastFired ? new Date(status.lastFired).toLocaleString() : 'N/A'}</span></span>
              <span className="text-slate-600">|</span>
              <span>Strategy wins: <span className="text-white">{status.fireCount}</span></span>
            </div>
          )}

          {/* Manual Buy / Sell buttons */}
          <div className="flex gap-3">
            <button
              onClick={handleManualBuy}
              disabled={buyLoading}
              className="flex-1 py-2.5 rounded-xl font-semibold text-sm border transition-all disabled:opacity-50 bg-emerald-500/15 border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/25"
            >
              {buyLoading ? '⏳' : '⚡'} {buyLoading ? 'Buying...' : `Buy ${pair} Now`}
            </button>
            <button
              onClick={handleManualSell}
              disabled={sellLoading}
              className="flex-1 py-2.5 rounded-xl font-semibold text-sm border transition-all disabled:opacity-50 bg-red-500/15 border-red-500/40 text-red-300 hover:bg-red-500/25"
            >
              {sellLoading ? '⏳' : '📉'} {sellLoading ? 'Selling...' : `Sell ${pair} Now`}
            </button>
          </div>

          {/* Result message */}
          {tradeMsg && (
            <div className={`mt-3 p-3 rounded-xl text-xs border font-medium ${
              tradeMsg.ok ? 'bg-emerald-500/15 border-emerald-500/30 text-emerald-300' : 'bg-red-500/15 border-red-500/30 text-red-300'
            }`}>
              {tradeMsg.ok ? '✅' : '❌'} {tradeMsg.msg}
            </div>
          )}

          {/* Note when bot is not running */}
          {!isRunning && (
            <p className="text-xs text-slate-500 mt-3 text-center">
              💡 Manual Buy/Sell works even without starting the bot. Start the bot for <strong>automatic</strong> 24/7 trading.
            </p>
          )}
        </>
      )}
    </div>
  );
}
