'use client';

import { useEffect, useState, useCallback } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';

interface SignalData {
  symbol: string;
  interval: string;
  summary?: {
    recommendation: string;
    buy: number;
    sell: number;
    neutral: number;
  };
  indicators?: Record<string, number | null>;
  error?: string;
}

const recommendationColors: Record<string, string> = {
  STRONG_BUY: 'text-emerald-400 bg-emerald-500/10 border-emerald-500/30',
  BUY: 'text-emerald-300 bg-emerald-500/10 border-emerald-500/20',
  NEUTRAL: 'text-slate-300 bg-slate-500/10 border-slate-500/20',
  SELL: 'text-red-300 bg-red-500/10 border-red-500/20',
  STRONG_SELL: 'text-red-400 bg-red-500/10 border-red-500/30',
};

const recEmoji: Record<string, string> = {
  STRONG_BUY: '🟢',
  BUY: '🟩',
  NEUTRAL: '⬜',
  SELL: '🟥',
  STRONG_SELL: '🔴',
};

interface Props {
  pair?: string;
  interval?: string;
  compact?: boolean;
}

export default function SignalsPanel({ pair = 'BTC/USDT', interval = '15m', compact = false }: Props) {
  const router = useRouter();
  const [signal, setSignal] = useState<SignalData | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedPair, setSelectedPair] = useState(pair);
  const [selectedInterval, setSelectedInterval] = useState(interval);
  const [availablePairs, setAvailablePairs] = useState<string[]>([]);

  // Buy modal state
  const [showBuyModal, setShowBuyModal] = useState(false);
  const [buyMode, setBuyMode] = useState<'paper' | 'live'>('paper');

  // Sell modal state
  const [showSellModal, setShowSellModal] = useState(false);
  const [sellMode, setSellMode] = useState<'paper' | 'live'>('paper');

  // Auto-buy toggle
  const [autoBuyEnabled, setAutoBuyEnabled] = useState(false);
  const [autoBuyLoading, setAutoBuyLoading] = useState(false);

  // Auto-sell toggle
  const [autoSellEnabled, setAutoSellEnabled] = useState(false);
  const [autoSellLoading, setAutoSellLoading] = useState(false);

  // Copy signal state
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    loadSignals();
    const timer = setInterval(loadSignals, 60000);
    return () => clearInterval(timer);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedPair, selectedInterval]);

  useEffect(() => {
    api.market.pairs().then((d) => {
      setAvailablePairs(d.pairs?.slice(0, 50) || []);
    }).catch(() => {});
  }, []);

  // Load current auto-trade state
  useEffect(() => {
    api.autotrade.settings.get().then((s: Record<string, unknown>) => {
      setAutoBuyEnabled(Boolean(s?.auto_trade_enabled));
      setAutoSellEnabled(Boolean(s?.auto_sell_enabled));
      if (s?.auto_trade_mode === 'live') setBuyMode('live');
      if (s?.auto_sell_mode === 'live') setSellMode('live');
    }).catch(() => {});
  }, []);

  const loadSignals = useCallback(async () => {
    setLoading(true);
    try {
      const data = await api.market.signals(selectedPair, selectedInterval) as SignalData;
      setSignal(data);
    } catch {
      setSignal({ symbol: selectedPair, interval: selectedInterval, error: 'Failed to load signals' });
    }
    setLoading(false);
  }, [selectedPair, selectedInterval]);

  const rec = signal?.summary?.recommendation || 'NEUTRAL';
  const colorClass = recommendationColors[rec] || recommendationColors.NEUTRAL;
  const isBullish = rec === 'STRONG_BUY' || rec === 'BUY';
  const isBearish = rec === 'STRONG_SELL' || rec === 'SELL';

  // ── Copy Signal ─────────────────────────────────────────────────────────────
  function buildSignalText(): string {
    const now = new Date().toUTCString().replace(' GMT', ' UTC');
    const emoji = recEmoji[rec] || '⬜';
    const ind = signal?.indicators || {};
    const lines = [
      `${emoji} ${rec.replace('_', ' ')} Signal — ${selectedPair} (${selectedInterval})`,
      `📊 TradingView TA: ${rec.replace('_', ' ')}`,
    ];
    if (signal?.summary) {
      lines.push(`   Buy: ${signal.summary.buy} | Neutral: ${signal.summary.neutral} | Sell: ${signal.summary.sell}`);
    }
    const indParts: string[] = [];
    if (ind.rsi != null) indParts.push(`RSI: ${(ind.rsi as number).toFixed(1)}`);
    if (ind.macd != null) indParts.push(`MACD: ${(ind.macd as number).toFixed(4)}`);
    if (ind.adx != null) indParts.push(`ADX: ${(ind.adx as number).toFixed(1)}`);
    if (ind.ema_20 != null) indParts.push(`EMA20: ${(ind.ema_20 as number).toFixed(2)}`);
    if (ind.bb_lower != null) indParts.push(`BB Lower: ${(ind.bb_lower as number).toFixed(2)}`);
    if (ind.bb_upper != null) indParts.push(`BB Upper: ${(ind.bb_upper as number).toFixed(2)}`);
    if (indParts.length > 0) lines.push(`📈 ${indParts.join(' | ')}`);
    lines.push(`🕒 ${now}`);
    lines.push(`📱 Sent via AutoTrade Hub`);
    return lines.join('\n');
  }

  async function copySignal() {
    try {
      await navigator.clipboard.writeText(buildSignalText());
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    } catch {
      const el = document.createElement('textarea');
      el.value = buildSignalText();
      document.body.appendChild(el);
      el.select();
      document.execCommand('copy');
      document.body.removeChild(el);
      setCopied(true);
      setTimeout(() => setCopied(false), 2500);
    }
  }

  // ── Auto-buy toggle ─────────────────────────────────────────────────────────
  async function toggleAutoBuy() {
    setAutoBuyLoading(true);
    const next = !autoBuyEnabled;
    try {
      await api.autotrade.settings.put({
        auto_trade_enabled: next,
        auto_trade_mode: buyMode,
        auto_trade_pairs: selectedPair,
        auto_trade_timeframe: selectedInterval,
      });
      setAutoBuyEnabled(next);
    } catch (e) {
      alert(`Auto-buy error: ${e}`);
    }
    setAutoBuyLoading(false);
  }

  // ── Auto-sell toggle ─────────────────────────────────────────────────────────
  async function toggleAutoSell() {
    setAutoSellLoading(true);
    const next = !autoSellEnabled;
    try {
      await api.autotrade.settings.put({
        auto_sell_enabled: next,
        auto_sell_mode: sellMode,
        auto_trade_pairs: selectedPair,
        auto_trade_timeframe: selectedInterval,
      });
      setAutoSellEnabled(next);
    } catch (e) {
      alert(`Auto-sell error: ${e}`);
    }
    setAutoSellLoading(false);
  }

  // ── Go to Buy/Sell trade page ─────────────────────────────────────────────────────────────────────
  function goTrade(action: 'buy' | 'sell') {
    const mode = action === 'buy' ? buyMode : sellMode;
    const base = mode === 'paper' ? '/paper-trade' : '/live';
    const q = new URLSearchParams({ pair: selectedPair, timeframe: selectedInterval, action });
    router.push(`${base}?${q.toString()}`);
    setShowBuyModal(false);
    setShowSellModal(false);
  }

  // ─────────────────────────────────────────────────────────────────────────────
  // COMPACT MODE
  if (compact) {
    return (
      <div className="card">
        <div className="flex items-center justify-between mb-2">
          <h3 className="font-semibold text-sm">TA Signals</h3>
          <span className="text-xs text-slate-500">{selectedPair} / {selectedInterval}</span>
        </div>
        {loading ? (
          <div className="text-slate-500 text-xs">Loading...</div>
        ) : signal?.error ? (
          <div className="text-red-400 text-xs">{signal.error}</div>
        ) : (
          <div className="flex items-center gap-2 flex-wrap">
            <div className={`inline-block px-3 py-1 rounded border text-sm font-bold ${colorClass}`}>
              {rec.replace('_', ' ')}
            </div>
            {isBullish && (
              <button
                onClick={() => setShowBuyModal(true)}
                className="px-3 py-1 rounded-lg text-xs font-semibold bg-emerald-500/20 border border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/30 transition-colors"
              >
                ⚡ Buy
              </button>
            )}
            {isBearish && (
              <button
                onClick={() => setShowSellModal(true)}
                className="px-3 py-1 rounded-lg text-xs font-semibold bg-red-500/20 border border-red-500/40 text-red-300 hover:bg-red-500/30 transition-colors"
              >
                📉 Sell
              </button>
            )}
            <button
              onClick={copySignal}
              className="px-2 py-1 rounded text-xs text-slate-400 hover:text-white border border-[#2a3a52] hover:border-brand-500 transition-colors"
              title="Copy signal"
            >
              {copied ? '✅' : '📋'}
            </button>
          </div>
        )}
        {showBuyModal && renderTradeModal('buy')}
        {showSellModal && renderTradeModal('sell')}
      </div>
    );
  }

  // ── Shared Trade Modal (Buy or Sell) ─────────────────────────────────────────
  function renderTradeModal(action: 'buy' | 'sell') {
    const isBuy = action === 'buy';
    const mode = isBuy ? buyMode : sellMode;
    const setMode = isBuy ? setBuyMode : setSellMode;
    const onClose = () => isBuy ? setShowBuyModal(false) : setShowSellModal(false);

    return (
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm"
        onClick={onClose}
      >
        <div
          className="bg-[#131c2e] border border-[#2a3a52] rounded-2xl p-6 w-full max-w-sm shadow-2xl"
          onClick={(e) => e.stopPropagation()}
        >
          {/* Header */}
          <div className={`inline-flex items-center gap-2 px-3 py-1 rounded-full text-xs font-semibold mb-3 ${
            isBuy ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/30' : 'bg-red-500/20 text-red-300 border border-red-500/30'
          }`}>
            {isBuy ? '⚡ BUY SIGNAL' : '📉 SELL SIGNAL'}
          </div>

          <h3 className="text-lg font-bold mb-1">{isBuy ? 'Buy' : 'Sell'} Signal — {selectedPair}</h3>
          <p className="text-slate-400 text-sm mb-1">
            TradingView says{' '}
            <span className={`font-bold ${isBullish ? 'text-emerald-400' : isBearish ? 'text-red-400' : 'text-slate-300'}`}>
              {rec.replace('_', ' ')}
            </span>{' '}
            on {selectedInterval}.
          </p>
          {signal?.summary && (
            <div className="flex gap-3 text-xs mb-4 text-slate-500">
              <span className="text-emerald-400">Buy: {signal.summary.buy}</span>
              <span>Neutral: {signal.summary.neutral}</span>
              <span className="text-red-400">Sell: {signal.summary.sell}</span>
            </div>
          )}

          {/* Mode toggle */}
          <div className="flex gap-2 mb-5">
            {(['paper', 'live'] as const).map((m) => (
              <button
                key={m}
                onClick={() => setMode(m)}
                className={`flex-1 py-2.5 rounded-lg text-sm font-semibold border transition-all ${
                  mode === m
                    ? m === 'paper'
                      ? 'bg-brand-600/30 border-brand-500 text-brand-300'
                      : 'bg-red-500/20 border-red-500/60 text-red-300'
                    : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white'
                }`}
              >
                {m === 'paper' ? '📄 Paper Trade' : '🔴 Live Trade'}
              </button>
            ))}
          </div>

          {mode === 'live' && (
            <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-red-400 text-xs">
              ⚠️ Live trading uses real money. You will need to confirm with &quot;CONFIRM&quot; on the next page.
            </div>
          )}

          {!isBuy && (
            <div className="mb-4 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-400 text-xs">
              📉 This will close any open {selectedPair} position or open a short. Check your open positions first.
            </div>
          )}

          <button
            onClick={() => goTrade(action)}
            className={`w-full py-3 rounded-xl font-bold text-sm mb-3 transition-all ${
              mode === 'paper'
                ? isBuy
                  ? 'bg-brand-600 hover:bg-brand-500 text-white'
                  : 'bg-amber-600 hover:bg-amber-500 text-white'
                : 'bg-red-600 hover:bg-red-500 text-white'
            }`}
          >
            {isBuy ? '⚡' : '📉'} Go to {mode === 'paper' ? 'Paper' : 'Live'} Trade →
          </button>

          <button onClick={onClose} className="w-full py-2 text-slate-400 hover:text-white text-sm transition-colors">
            Cancel
          </button>
        </div>
      </div>
    );
  }

  // ── FULL VIEW ─────────────────────────────────────────────────────────────────
  return (
    <div className="card">
      <h2 className="text-lg font-semibold mb-4">TradingView Technical Analysis</h2>

      {/* Selectors */}
      <div className="flex gap-3 mb-6 flex-wrap">
        <div className="flex-1 min-w-[140px]">
          <label className="label">Pair</label>
          <select
            className="input"
            value={selectedPair}
            onChange={(e) => setSelectedPair(e.target.value)}
          >
            <option value="BTC/USDT">BTC/USDT</option>
            <option value="ETH/USDT">ETH/USDT</option>
            <option value="SOL/USDT">SOL/USDT</option>
            <option value="XRP/USDT">XRP/USDT</option>
            <option value="BNB/USDT">BNB/USDT</option>
            <option value="DOGE/USDT">DOGE/USDT</option>
            <option value="ADA/USDT">ADA/USDT</option>
            <option value="AVAX/USDT">AVAX/USDT</option>
            {availablePairs
              .filter((p) => !['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'XRP/USDT', 'BNB/USDT', 'DOGE/USDT', 'ADA/USDT', 'AVAX/USDT'].includes(p))
              .map((p) => (
                <option key={p} value={p}>{p}</option>
              ))}
          </select>
        </div>
        <div>
          <label className="label">Interval</label>
          <select
            className="input"
            value={selectedInterval}
            onChange={(e) => setSelectedInterval(e.target.value)}
          >
            {['1m', '5m', '15m', '30m', '1h', '2h', '4h', '1d', '1w'].map((tf) => (
              <option key={tf} value={tf}>{tf}</option>
            ))}
          </select>
        </div>
        <div className="flex items-end">
          <button onClick={loadSignals} className="btn-secondary text-sm">
            Refresh
          </button>
        </div>
      </div>

      {loading ? (
        <div className="text-center py-8 text-slate-500">Loading signals...</div>
      ) : signal?.error ? (
        <div className="text-center py-8 text-red-400">{signal.error}</div>
      ) : (
        <>
          {/* Main Recommendation */}
          <div className="text-center mb-6">
            <div className={`inline-block px-6 py-3 rounded-xl border text-2xl font-bold ${colorClass}`}>
              {rec.replace('_', ' ')}
            </div>
            <div className="flex justify-center gap-6 mt-3 text-sm">
              <span className="text-emerald-400">Buy: {signal?.summary?.buy || 0}</span>
              <span className="text-slate-400">Neutral: {signal?.summary?.neutral || 0}</span>
              <span className="text-red-400">Sell: {signal?.summary?.sell || 0}</span>
            </div>

            {/* ── Action Buttons Row ── */}
            <div className="flex justify-center gap-3 mt-5 flex-wrap">

              {/* BUY NOW — active when bullish, dimmed otherwise */}
              <button
                onClick={() => setShowBuyModal(true)}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm border transition-all ${
                  isBullish
                    ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/35 shadow-lg shadow-emerald-500/10'
                    : 'bg-slate-700/30 border-slate-600/30 text-slate-500 hover:text-slate-300 hover:border-slate-500'
                }`}
                title={isBullish ? 'Signal active — buy now' : 'No active buy signal — proceed with caution'}
              >
                <span className="text-base">⚡</span>
                Buy Now
                {isBullish && (
                  <span className="text-[10px] bg-emerald-500/30 px-1.5 py-0.5 rounded text-emerald-300">Signal active</span>
                )}
              </button>

              {/* SELL NOW — active when bearish, dimmed otherwise */}
              <button
                onClick={() => setShowSellModal(true)}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm border transition-all ${
                  isBearish
                    ? 'bg-red-500/20 border-red-500/50 text-red-300 hover:bg-red-500/35 shadow-lg shadow-red-500/10'
                    : 'bg-slate-700/30 border-slate-600/30 text-slate-500 hover:text-slate-300 hover:border-slate-500'
                }`}
                title={isBearish ? 'Signal active — sell / close position now' : 'No active sell signal — proceed with caution'}
              >
                <span className="text-base">📉</span>
                Sell Now
                {isBearish && (
                  <span className="text-[10px] bg-red-500/30 px-1.5 py-0.5 rounded text-red-300">Signal active</span>
                )}
              </button>

              {/* AUTO-BUY TOGGLE */}
              <button
                onClick={toggleAutoBuy}
                disabled={autoBuyLoading}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm border transition-all ${
                  autoBuyEnabled
                    ? 'bg-emerald-500/20 border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/30'
                    : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-slate-500'
                }`}
                title={autoBuyEnabled ? 'Auto-Buy is ON — click to disable' : 'Enable Auto-Buy on bullish signals'}
              >
                {autoBuyLoading ? <span className="animate-spin">⟳</span> : <span>🤖</span>}
                {autoBuyEnabled ? 'Auto-Buy ON' : 'Auto-Buy OFF'}
                {autoBuyEnabled && <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />}
              </button>

              {/* AUTO-SELL TOGGLE */}
              <button
                onClick={toggleAutoSell}
                disabled={autoSellLoading}
                className={`flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm border transition-all ${
                  autoSellEnabled
                    ? 'bg-red-500/20 border-red-500/50 text-red-300 hover:bg-red-500/30'
                    : 'bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-slate-500'
                }`}
                title={autoSellEnabled ? 'Auto-Sell is ON — click to disable' : 'Enable Auto-Sell on bearish signals'}
              >
                {autoSellLoading ? <span className="animate-spin">⟳</span> : <span>🔻</span>}
                {autoSellEnabled ? 'Auto-Sell ON' : 'Auto-Sell OFF'}
                {autoSellEnabled && <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />}
              </button>

              {/* COPY SIGNAL */}
              <button
                onClick={copySignal}
                className="flex items-center gap-2 px-5 py-2.5 rounded-xl font-semibold text-sm border bg-[#1a2236] border-[#2a3a52] text-slate-400 hover:text-white hover:border-brand-500 transition-all"
                title="Copy signal text to clipboard"
              >
                {copied ? <>✅ <span>Copied!</span></> : <>📋 <span>Copy Signal</span></>}
              </button>
            </div>

            {/* Auto status banners */}
            {(autoBuyEnabled || autoSellEnabled) && (
              <div className="mt-4 mx-auto max-w-lg space-y-2">
                {autoBuyEnabled && (
                  <div className="p-3 rounded-xl bg-emerald-500/10 border border-emerald-500/30 text-emerald-300 text-xs">
                    🤖 Auto-buy is <strong>ON</strong> — engine will buy {selectedPair} on {selectedInterval} when a bullish signal is detected.
                    Mode: <strong>{buyMode}</strong>. Click Auto-Buy to stop.
                  </div>
                )}
                {autoSellEnabled && (
                  <div className="p-3 rounded-xl bg-red-500/10 border border-red-500/30 text-red-300 text-xs">
                    🔻 Auto-sell is <strong>ON</strong> — engine will sell {selectedPair} on {selectedInterval} when a bearish signal is detected.
                    Mode: <strong>{sellMode}</strong>. Click Auto-Sell to stop.
                  </div>
                )}
              </div>
            )}
          </div>

          {/* Signal Gauge Bar */}
          {signal?.summary && (
            <div className="mb-6">
              <div className="flex justify-between text-xs text-slate-500 mb-1">
                <span>Bullish</span>
                <span>Bearish</span>
              </div>
              <div className="flex h-3 rounded-full overflow-hidden bg-[#0a0f1c]">
                <div
                  className="bg-emerald-500 transition-all"
                  style={{ width: `${(signal.summary.buy / (signal.summary.buy + signal.summary.neutral + signal.summary.sell || 1)) * 100}%` }}
                />
                <div
                  className="bg-slate-500 transition-all"
                  style={{ width: `${(signal.summary.neutral / (signal.summary.buy + signal.summary.neutral + signal.summary.sell || 1)) * 100}%` }}
                />
                <div
                  className="bg-red-500 transition-all"
                  style={{ width: `${(signal.summary.sell / (signal.summary.buy + signal.summary.neutral + signal.summary.sell || 1)) * 100}%` }}
                />
              </div>
              <div className="flex justify-between text-xs text-slate-600 mt-1">
                <span>{signal.summary.buy} Buy</span>
                <span>{signal.summary.neutral} Neutral</span>
                <span>{signal.summary.sell} Sell</span>
              </div>
            </div>
          )}

          {/* Key Indicators */}
          {signal?.indicators && (
            <div>
              <h3 className="text-sm font-medium text-slate-400 mb-3">Key Indicators</h3>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-5 gap-3">
                {[
                  { key: 'rsi', label: 'RSI (14)', format: (v: number) => v.toFixed(1), color: (v: number) => v > 70 ? 'text-red-400' : v < 30 ? 'text-emerald-400' : 'text-white' },
                  { key: 'macd', label: 'MACD', format: (v: number) => v.toFixed(4), color: (v: number) => v > 0 ? 'text-emerald-400' : 'text-red-400' },
                  { key: 'macd_signal', label: 'MACD Signal', format: (v: number) => v.toFixed(4), color: () => 'text-white' },
                  { key: 'adx', label: 'ADX', format: (v: number) => v.toFixed(1), color: (v: number) => v > 25 ? 'text-emerald-400' : 'text-slate-400' },
                  { key: 'atr', label: 'ATR', format: (v: number) => v.toFixed(4), color: () => 'text-white' },
                  { key: 'ema_20', label: 'EMA 20', format: (v: number) => v.toFixed(2), color: () => 'text-white' },
                  { key: 'sma_50', label: 'SMA 50', format: (v: number) => v.toFixed(2), color: () => 'text-white' },
                  { key: 'bb_upper', label: 'BB Upper', format: (v: number) => v.toFixed(2), color: () => 'text-red-300' },
                  { key: 'bb_lower', label: 'BB Lower', format: (v: number) => v.toFixed(2), color: () => 'text-emerald-300' },
                  { key: 'volume', label: 'Volume', format: (v: number) => v >= 1e6 ? `${(v / 1e6).toFixed(1)}M` : v >= 1e3 ? `${(v / 1e3).toFixed(1)}K` : v.toFixed(0), color: () => 'text-white' },
                ].map(({ key, label, format, color }) => {
                  const val = signal.indicators?.[key];
                  if (val === null || val === undefined) return null;
                  return (
                    <div key={key} className="bg-[#0a0f1c] rounded-lg p-3 border border-[#1a2236]">
                      <p className="text-xs text-slate-500 mb-1">{label}</p>
                      <p className={`text-sm font-mono font-medium ${color(val as number)}`}>{format(val as number)}</p>
                    </div>
                  );
                })}
              </div>

              {/* RSI interpretation hint */}
              {signal.indicators?.rsi != null && (
                <div className={`mt-3 text-xs px-3 py-2 rounded-lg border ${
                  (signal.indicators.rsi as number) > 70
                    ? 'bg-red-500/10 border-red-500/20 text-red-400'
                    : (signal.indicators.rsi as number) < 30
                    ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-400'
                    : 'bg-slate-500/10 border-slate-500/20 text-slate-400'
                }`}>
                  RSI {(signal.indicators.rsi as number).toFixed(1)} —{' '}
                  {(signal.indicators.rsi as number) > 70
                    ? '⚠️ Overbought zone. Consider selling or wait for pullback.'
                    : (signal.indicators.rsi as number) < 30
                    ? '✅ Oversold zone. Potential buying opportunity.'
                    : 'Neutral zone. No extreme conditions.'}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {/* Modals */}
      {showBuyModal && renderTradeModal('buy')}
      {showSellModal && renderTradeModal('sell')}
    </div>
  );
}
