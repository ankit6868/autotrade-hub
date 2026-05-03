'use client';

import { useEffect, useRef, memo } from 'react';

interface Props {
  symbol?: string;       // e.g. "KUCOIN:BTCUSDT"
  interval?: string;     // e.g. "15"
  /** Optional fixed pixel height. If omitted, the chart fills its container
   *  and falls back to a responsive min-height (300px on phones, ~520px on
   *  desktop). Pass a number when you specifically need a fixed size. */
  height?: number;
  theme?: 'dark' | 'light';
  showToolbar?: boolean;
}

function TradingViewWidget({
  symbol = 'KUCOIN:BTCUSDT',
  interval = '15',
  height,
  theme = 'dark',
  showToolbar = true,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const scriptRef = useRef<HTMLScriptElement | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;

    // Clear previous widget but preserve the inner widget div TradingView needs
    containerRef.current.innerHTML =
      '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';

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
        containerRef.current.innerHTML =
          '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';
      }
    };
  }, [symbol, interval, theme, showToolbar]);

  // Fluid sizing: when no explicit height is given, use a responsive
  // min-height so the chart looks decent on phones AND desktops.
  const style: React.CSSProperties = height
    ? { height, width: '100%' }
    : { width: '100%' };

  const fluidClass = height
    ? ''
    : 'min-h-[320px] sm:min-h-[420px] lg:min-h-[520px] h-[60vh] max-h-[720px]';

  return (
    <div
      className={`tradingview-widget-container ${fluidClass}`}
      ref={containerRef}
      style={style}
    >
      <div className="tradingview-widget-container__widget" style={{ height: '100%', width: '100%' }} />
    </div>
  );
}

export default memo(TradingViewWidget);
