'use client';

import { useEffect, useRef } from 'react';
import { createChart, ColorType, CandlestickData, Time } from 'lightweight-charts';

interface Props {
  data: { timestamp: number; open: number; close: number; high: number; low: number; volume: number }[];
  height?: number;
}

export default function CandlestickChart({ data, height = 400 }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<typeof createChart> | null>(null);

  useEffect(() => {
    if (!containerRef.current || data.length === 0) return;

    // Clean up previous chart
    if (chartRef.current) {
      chartRef.current.remove();
      chartRef.current = null;
    }

    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: '#0a0f1c' },
        textColor: '#64748b',
      },
      grid: {
        vertLines: { color: '#1e293b' },
        horzLines: { color: '#1e293b' },
      },
      crosshair: {
        mode: 0,
      },
      rightPriceScale: {
        borderColor: '#2a3a52',
      },
      timeScale: {
        borderColor: '#2a3a52',
        timeVisible: true,
        secondsVisible: false,
      },
    });

    chartRef.current = chart;

    // Candlestick series
    const candleSeries = chart.addCandlestickSeries({
      upColor: '#22c55e',
      downColor: '#ef4444',
      borderUpColor: '#22c55e',
      borderDownColor: '#ef4444',
      wickUpColor: '#22c55e',
      wickDownColor: '#ef4444',
    });

    const candleData: CandlestickData[] = data
      .sort((a, b) => a.timestamp - b.timestamp)
      .map((d) => ({
        time: d.timestamp as Time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      }));

    candleSeries.setData(candleData);

    // Volume series
    const volumeSeries = chart.addHistogramSeries({
      color: '#3391ff',
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });

    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });

    const volumeData = data
      .sort((a, b) => a.timestamp - b.timestamp)
      .map((d) => ({
        time: d.timestamp as Time,
        value: d.volume,
        color: d.close >= d.open ? 'rgba(34,197,94,0.3)' : 'rgba(239,68,68,0.3)',
      }));

    volumeSeries.setData(volumeData);

    chart.timeScale().fitContent();

    // Resize handler
    const handleResize = () => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({ width: containerRef.current.clientWidth });
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      if (chartRef.current) {
        chartRef.current.remove();
        chartRef.current = null;
      }
    };
  }, [data, height]);

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center text-slate-500 text-sm" style={{ height }}>
        No chart data available. Select a pair and load candles.
      </div>
    );
  }

  return <div ref={containerRef} className="w-full rounded-lg overflow-hidden" />;
}
