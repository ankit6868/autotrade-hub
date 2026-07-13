'use client';

import { useEffect, useRef, memo } from 'react';

interface Props {
  /** Trading pair, e.g. "BTC/USDT". Mapped to the KuCoin perp symbol. */
  pair?: string;
  /** Explicit TradingView symbol; overrides `pair` when provided. */
  symbol?: string;
  /** Our timeframe token ("1m","5m","15m","30m","1h","4h","1d"). */
  interval?: string;
  theme?: 'dark' | 'light';
}

// our tf → TradingView interval code
const TV_INTERVAL: Record<string, string> = {
  '1m': '1', '5m': '5', '15m': '15', '30m': '30',
  '1h': '60', '4h': '240', '1d': 'D',
};

// "BTC/USDT" → "KUCOIN:BTCUSDT.P"  (KuCoin perpetual, native on TradingView)
function toTvSymbol(pair: string): string {
  const [base, quote] = (pair || 'BTC/USDT').split('/');
  return `KUCOIN:${(base || 'BTC').toUpperCase()}${(quote || 'USDT').toUpperCase()}.P`;
}

/**
 * The REAL TradingView Advanced Real-Time Chart — the same engine KuCoin's own
 * terminal uses. Tick-level real-time, all TradingView indicators + drawing
 * tools. It's a sealed iframe, so it can't show our custom entry/TP/SL markers
 * (those live on the "Advanced" chart). Symbol is locked to the KuCoin perp for
 * the current pair; changing the pair re-mounts the widget.
 */
function TradingViewWidget({ pair = 'BTC/USDT', symbol, interval = '15m', theme = 'dark' }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const tvSymbol = symbol || toTvSymbol(pair);

  useEffect(() => {
    const host = containerRef.current;
    if (!host) return;

    // local timezone so the axis matches the user's clock (like KuCoin), not UTC
    let tz = 'Etc/UTC';
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Etc/UTC'; } catch { /* keep UTC */ }

    host.innerHTML = '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol: tvSymbol,
      interval: TV_INTERVAL[interval] || '15',
      timezone: tz,
      theme,
      style: '1',                 // candles
      locale: 'en',
      enable_publishing: false,
      allow_symbol_change: false, // stay on the pair the terminal is trading
      hide_legend: false,
      save_image: false,
      calendar: false,
      support_host: 'https://www.tradingview.com',
      studies: ['STD;RSI', 'STD;MACD'],
    });
    host.appendChild(script);

    return () => { host.innerHTML = ''; };
  }, [tvSymbol, interval, theme]);

  return (
    <div
      className="tradingview-widget-container h-full w-full"
      ref={containerRef}
      style={{ height: '100%', width: '100%' }}
    />
  );
}

export default memo(TradingViewWidget);
