'use client';

import { useEffect, useRef, memo } from 'react';

interface Props {
  symbol?: string;       // e.g. "KUCOIN:BTCUSDT"
  interval?: string;     // e.g. "15"
  height?: number;
  theme?: 'dark' | 'light';
  showToolbar?: boolean;
}

function TradingViewWidget({
  symbol = 'KUCOIN:BTCUSDT',
  interval = '15',
  height = 400,
  theme = 'dark',
  showToolbar = true,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const scriptRef = useRef<HTMLScriptElement | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // Clear previous widget but preserve the inner widget div TradingView needs
    containerRef.current.innerHTML = '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.innerHTML = JSON.stringify({
      autosize: true,
      symbol,
      interval,
      timezone: 'Etc/UTC',
      theme,
      style: '1',
      locale: 'en',
      allow_symbol_change: true,
      calendar: false,
      support_host: 'https://www.tradingview.com',
      hide_top_toolbar: !showToolbar,
      hide_legend: false,
      save_image: false,
      studies: [
        'STD;RSI',
        'STD;MACD',
        'STD;Bollinger_Bands',
      ],
    });

    containerRef.current.appendChild(script);
    scriptRef.current = script;

    return () => {
      if (containerRef.current) {
        containerRef.current.innerHTML = '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';
      }
    };
  }, [symbol, interval, theme, showToolbar]);

  return (
    <div
      className="tradingview-widget-container"
      ref={containerRef}
      style={{ height, width: '100%' }}
    >
      <div className="tradingview-widget-container__widget" style={{ height: '100%', width: '100%' }} />
    </div>
  );
}

export default memo(TradingViewWidget);
