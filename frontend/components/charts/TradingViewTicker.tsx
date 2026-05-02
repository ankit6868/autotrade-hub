'use client';

import { useEffect, useRef } from 'react';

export default function TradingViewTicker() {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    // Preserve the inner widget div that TradingView queries via querySelector
    containerRef.current.innerHTML = '<div class="tradingview-widget-container__widget"></div>';

    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-ticker-tape.js';
    script.async = true;
    script.innerHTML = JSON.stringify({
      symbols: [
        { proName: 'KUCOIN:BTCUSDT', title: 'BTC/USDT' },
        { proName: 'KUCOIN:ETHUSDT', title: 'ETH/USDT' },
        { proName: 'KUCOIN:SOLUSDT', title: 'SOL/USDT' },
        { proName: 'KUCOIN:XRPUSDT', title: 'XRP/USDT' },
        { proName: 'KUCOIN:BNBUSDT', title: 'BNB/USDT' },
        { proName: 'KUCOIN:DOGEUSDT', title: 'DOGE/USDT' },
        { proName: 'KUCOIN:ADAUSDT', title: 'ADA/USDT' },
        { proName: 'KUCOIN:AVAXUSDT', title: 'AVAX/USDT' },
        { proName: 'KUCOIN:DOTUSDT', title: 'DOT/USDT' },
        { proName: 'KUCOIN:MATICUSDT', title: 'MATIC/USDT' },
      ],
      showSymbolLogo: true,
      isTransparent: true,
      displayMode: 'adaptive',
      colorTheme: 'dark',
      locale: 'en',
    });

    containerRef.current.appendChild(script);

    return () => {
      if (containerRef.current) containerRef.current.innerHTML = '<div class="tradingview-widget-container__widget"></div>';
    };
  }, []);

  return (
    <div className="tradingview-widget-container" ref={containerRef} style={{ width: '100%' }}>
      <div className="tradingview-widget-container__widget" />
    </div>
  );
}
