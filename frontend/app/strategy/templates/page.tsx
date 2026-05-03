'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';

const templateInfo: Record<string, { desc: string; rules: string; timeframe: string; stoploss: string }> = {
  RsiBollingerStrategy: {
    desc: 'Buy when RSI is oversold and price is below lower Bollinger Band',
    rules: 'Buy: RSI(14) < 30 AND price < BB lower\nSell: RSI > 70 OR price > BB upper',
    timeframe: '15m',
    stoploss: '3%',
  },
  MacdCrossoverStrategy: {
    desc: 'Trade MACD crossovers with histogram confirmation',
    rules: 'Buy: MACD crosses above signal, histogram positive\nSell: MACD crosses below signal',
    timeframe: '1h',
    stoploss: '2.5%',
  },
  EmaScalpingStrategy: {
    desc: 'Fast EMA crossover scalping with volume confirmation',
    rules: 'Buy: EMA(9) > EMA(21), volume > 1.5x avg\nSell: EMA(9) < EMA(21)',
    timeframe: '5m',
    stoploss: '1.5%',
  },
  DcaAccumulationStrategy: {
    desc: 'Dollar-cost averaging with profit target exit',
    rules: 'Buy: Every interval regardless of price\nSell: Total profit > 10%',
    timeframe: '4h',
    stoploss: 'None',
  },
};

export default function TemplatesPage() {
  const router = useRouter();
  const [templates, setTemplates] = useState<{ file: string; name: string; code: string }[]>([]);
  const [selectedCode, setSelectedCode] = useState('');

  useEffect(() => {
    api.strategy.templates().then((data) => setTemplates(data.templates)).catch(() => {});
  }, []);

  async function useTemplate(tmpl: { name: string; code: string }) {
    try {
      const formData = new FormData();
      formData.append('text', tmpl.code);
      formData.append('name', tmpl.name);
      // Templates are already valid freqtrade code — bypass the AI parser.
      formData.append('skip_ai', 'true');
      const data = await api.strategy.upload(formData);
      if (data.error) {
        alert(`Error: ${data.error}`);
        return;
      }
      if (data.id) {
        router.push(`/strategy/editor?id=${data.id}`);
      }
    } catch (e) {
      alert(`Error: ${e}`);
    }
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="heading-xl mb-2">Strategy Templates</h1>
      <p className="text-slate-400 mb-6 sm:mb-8 text-sm sm:text-base">Pre-built strategies ready to use — no AI parsing needed</p>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 sm:gap-6 mb-6 sm:mb-8">
        {templates.map((tmpl) => {
          const info = templateInfo[tmpl.name] || { desc: '', rules: '', timeframe: '', stoploss: '' };
          return (
            <div key={tmpl.name} className="card hover:border-brand-500/50 transition-colors">
              <h3 className="text-lg font-semibold mb-2">{tmpl.name}</h3>
              <p className="text-sm text-slate-400 mb-3">{info.desc}</p>
              <pre className="text-xs text-slate-500 bg-[#0a0f1c] rounded p-3 mb-3 whitespace-pre-wrap">
                {info.rules}
              </pre>
              <div className="flex gap-4 text-xs text-slate-500 mb-4">
                <span>Timeframe: {info.timeframe}</span>
                <span>Stop-loss: {info.stoploss}</span>
              </div>
              <div className="flex gap-2">
                <button onClick={() => setSelectedCode(tmpl.code)} className="btn-secondary text-sm">
                  View Code
                </button>
                <button onClick={() => useTemplate(tmpl)} className="btn-primary text-sm">
                  Use Template
                </button>
              </div>
            </div>
          );
        })}

        {templates.length === 0 && (
          <div className="col-span-2 card text-center py-12">
            <p className="text-slate-400">No templates found. Make sure the backend is running and templates exist in <code>strategies/templates/</code></p>
          </div>
        )}
      </div>

      {selectedCode && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-lg font-semibold">Template Code</h2>
            <button onClick={() => setSelectedCode('')} className="text-slate-400 hover:text-white text-sm">
              Close
            </button>
          </div>
          <pre className="bg-[#0a0f1c] rounded-lg p-4 overflow-x-auto text-sm font-mono text-slate-300 max-h-[500px] overflow-y-auto">
            {selectedCode}
          </pre>
        </div>
      )}
    </div>
  );
}
