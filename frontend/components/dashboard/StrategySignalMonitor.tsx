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
  isLive?: boolean;        // true = live trading, false/undefined = paper
  isFutures?: boolean;     // true = futures engine (isolated from spot)
  manualStakePct?: number; // % of balance to use for manual entries (default 5)
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
    const crossedUp   = macd > macdS;
    const crossedDown = macd < macdS;
    const macdPos     = macd > 0;
    const longSig     = crossedUp && macdPos;
    const shortSig    = crossedDown && !macdPos;
    conditions = [
      { label: 'MACD crossed above signal → LONG',  met: crossedUp,   value: `${macd?.toFixed(4) || '?'}`, required: `> ${macdS?.toFixed(4) || '?'}` },
      { label: 'MACD crossed below signal → SHORT', met: crossedDown, value: `${macd?.toFixed(4) || '?'}`, required: `< ${macdS?.toFixed(4) || '?'}` },
      { label: 'Market signal',                      met: rec === 'BUY' || rec === 'SELL', value: rec, required: 'BUY or SELL' },
    ];
    ready = longSig || shortSig;
  } else if (n.includes('rsi') || n.includes('bollinger')) {
    const oversold   = rsi < 30;
    const overbought = rsi > 70;
    const bbUpper    = ind.bb_upper as number;
    const belowBB    = price && bbLower ? price < bbLower : false;
    const aboveBB    = price && bbUpper ? price > bbUpper : false;
    conditions = [
      { label: 'RSI < 30 AND price < BB Lower → LONG',  met: oversold && belowBB,   value: `RSI ${rsi?.toFixed(1) || '?'} · ${price?.toFixed(2) || '?'}`, required: `RSI<30 & Price<${bbLower?.toFixed(2) || '?'}` },
      { label: 'RSI > 70 AND price > BB Upper → SHORT', met: overbought && aboveBB, value: `RSI ${rsi?.toFixed(1) || '?'} · ${price?.toFixed(2) || '?'}`, required: `RSI>70 & Price>${bbUpper?.toFixed(2) || '?'}` },
    ];
    ready = (oversold && belowBB) || (overbought && aboveBB);
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
  } else if (n.includes('simple') || n.includes('target')) {
    // SimpleTargetStrategy — bidirectional: LONG on oversold, SHORT on overbought
    const oversold    = rsi < 38;
    const mildDip     = rsi < 55 && (price && ema20 ? price <= ema20 * 1.005 : false);
    const longReady   = oversold || mildDip;
    const overbought  = rsi > 72;
    const mildTop     = rsi > 65 && (price && ema20 ? price > ema20 * 1.005 : false);
    const shortReady  = overbought || mildTop;
    const entryReady  = longReady || shortReady;
    const signalDir   = longReady ? '📈 LONG' : shortReady ? '📉 SHORT' : '—';
    conditions = [
      {
        label: 'RSI < 38 (strongly oversold) → LONG',
        met:   oversold,
        value: rsi?.toFixed(1) || '?',
        required: '< 38',
      },
      {
        label: 'RSI < 55 AND price near EMA-20 → LONG',
        met:   mildDip,
        value: `RSI ${rsi?.toFixed(1) || '?'} · Price ${price?.toFixed(0) || '?'}`,
        required: `RSI<55 & Price ≤ ${ema20?.toFixed(0) || '?'} × 1.005`,
      },
      {
        label: 'RSI > 72 (strongly overbought) → SHORT',
        met:   overbought,
        value: rsi?.toFixed(1) || '?',
        required: '> 72',
      },
      {
        label: 'RSI > 65 AND price above EMA-20 → SHORT',
        met:   mildTop,
        value: `RSI ${rsi?.toFixed(1) || '?'} · Price ${price?.toFixed(0) || '?'}`,
        required: `RSI>65 & Price > ${ema20?.toFixed(0) || '?'} × 1.005`,
      },
      {
        label: `Signal direction: ${signalDir}`,
        met:   entryReady,
        value: entryReady ? signalDir : 'NEUTRAL — waiting for setup',
        required: 'LONG or SHORT entry',
      },
    ];
    ready = entryReady;
  } else {
    // Generic fallback
    conditions = [
      { label: 'Market signal', met: rec === 'BUY' || rec === 'STRONG_BUY', value: rec, required: 'BUY or STRONG_BUY' },
    ];
    ready = rec === 'BUY' || rec === 'STRONG_BUY';
  }

  return { ready, conditions, recommendation: rec, fireCount: tradeCount, lastFired: lastTrade };
}

export default function StrategySignalMonitor({ strategyName, pair, timeframe, isRunning, isLive = false, isFutures = false, manualStakePct = 5, onManualBuy, onManualSell }: Props) {
  const [status, setStatus] = useState<SignalStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [buyLoading, setBuyLoading] = useState(false);
  const [sellLoading, setSellLoading] = useState(false);
  const [tradeMsg, setTradeMsg] = useState<{ ok: boolean; msg: string } | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const mode = isLive ? 'live' : 'paper';
      const [sigData, histData] = await Promise.all([
        api.market.signals(pair, timeframe) as Promise<{ summary?: { recommendation: string }; indicators?: Record<string, number | null> }>,
        // Route to futures history when on a futures page — keeps data isolated
        isFutures
          ? (api.futures.history({ mode, limit: '100' }) as Promise<{ trades: Array<{ profit_abs: number; exit_time?: string }> }>)
          : (api.trade.history({ mode, limit: '100' }) as Promise<{ trades: Array<{ profit_abs: number; close_date?: string; exit_time?: string }> }>),
      ]);
      const rec = sigData?.summary?.recommendation || 'NEUTRAL';
      const allTrades = histData?.trades || [];
      const lastTrade = allTrades.length > 0
        ? (allTrades[0]?.exit_time || (allTrades[0] as any)?.close_date || null)
        : null;
      setStatus(buildStatus(strategyName, sigData?.indicators, rec, allTrades.length, lastTrade));
    } catch {
      setStatus(null);
    }
    setLoading(false);
  }, [strategyName, pair, timeframe, isLive, isFutures]);

  useEffect(() => {
    load();
    const t = setInterval(load, 60000);
    return () => clearInterval(t);
  }, [load]);

  async function handleManualBuy() {
    setBuyLoading(true);
    try {
      // isFutures=true → futures engine (market_type='futures', isolated DB rows)
      // isFutures=false → spot engine (market_type='spot')
      const res = isFutures
        ? await api.futures.manualEntry(pair, 'long', manualStakePct)
        : await api.trade.manualEntry(pair, 'long');
      if (res?.entered) {
        const liqInfo = res.liq ? ` | Liq: ${res.liq}` : '';
        const levInfo = res.leverage ? ` (${res.leverage}x)` : '';
        setTradeMsg({ ok: true, msg: `Bought ${pair}${levInfo} @ ${res.entry} | SL: ${res.sl} | TP: ${res.tp}${liqInfo}` });
        onManualBuy?.();
        load();
      } else {
        setTradeMsg({ ok: false, msg: res?.error || 'Buy failed' });
      }
    } catch (e) {
      setTradeMsg({ ok: false, msg: String(e) });
    }
    setBuyLoading(false);
    setTimeout(() => setTradeMsg(null), 6000);
  }

  async function handleManualSell() {
    setSellLoading(true);
    try {
      const res = isFutures
        ? await api.futures.manualEntry(pair, 'short', manualStakePct)
        : await api.trade.manualEntry(pair, 'short');
      if (res?.entered) {
        const liqInfo = res.liq ? ` | Liq: ${res.liq}` : '';
        const levInfo = res.leverage ? ` (${res.leverage}x)` : '';
        setTradeMsg({ ok: true, msg: `Sold ${pair}${levInfo} @ ${res.entry} | SL: ${res.sl} | TP: ${res.tp}${liqInfo}` });
        onManualSell?.();
        load();
      } else {
        setTradeMsg({ ok: false, msg: res?.error || 'Sell failed' });
      }
    } catch (e) {
      setTradeMsg({ ok: false, msg: String(e) });
    }
    setSellLoading(false);
    setTimeout(() => setTradeMsg(null), 6000);
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

          {/* Manual Long / Short (Futures) or Buy / Sell (Spot) buttons */}
          <div className="flex gap-3">
            <button
              onClick={handleManualBuy}
              disabled={buyLoading}
              className="flex-1 py-2.5 rounded-xl font-semibold text-sm border transition-all disabled:opacity-50 bg-emerald-500/15 border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/25"
            >
              {buyLoading ? '⏳' : '⚡'} {buyLoading ? (isFutures ? 'Opening Long...' : 'Buying...') : (isFutures ? `Long ${pair}` : `Buy ${pair} Now`)}
            </button>
            <button
              onClick={handleManualSell}
              disabled={sellLoading}
              className="flex-1 py-2.5 rounded-xl font-semibold text-sm border transition-all disabled:opacity-50 bg-red-500/15 border-red-500/40 text-red-300 hover:bg-red-500/25"
            >
              {sellLoading ? '⏳' : '📉'} {sellLoading ? (isFutures ? 'Opening Short...' : 'Selling...') : (isFutures ? `Short ${pair}` : `Sell ${pair} Now`)}
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
