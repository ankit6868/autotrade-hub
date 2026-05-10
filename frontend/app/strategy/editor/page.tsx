'use client';

import { useEffect, useState, Suspense } from 'react';
import { useSearchParams } from 'next/navigation';
import { api } from '@/lib/api';
import LoadingSpinner from '@/components/ui/LoadingSpinner';
import dynamic from 'next/dynamic';

const MonacoEditor = dynamic(() => import('@monaco-editor/react'), { ssr: false });

function EditorContent() {
  const searchParams = useSearchParams();
  const strategyId = searchParams.get('id');

  const [code, setCode] = useState('');
  const [name, setName] = useState('');
  const [timeframe, setTimeframe] = useState('15m');
  const [stoploss, setStoploss] = useState(3);
  const [takeProfit, setTakeProfit] = useState(1.5);
  const [leverage, setLeverage] = useState(10);
  const [futuresEnabled, setFuturesEnabled] = useState(false);
  const [pairs, setPairs] = useState('BTC/USDT');
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<Record<string, unknown> | null>(null);
  const [aiPrompt, setAiPrompt] = useState('');
  const [aiLoading, setAiLoading] = useState(false);
  const [strategies, setStrategies] = useState<Record<string, unknown>[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(strategyId ? Number(strategyId) : null);
  const [msg, setMsg] = useState('');
  const [deleteConfirm, setDeleteConfirm] = useState(false);

  useEffect(() => {
    loadStrategies();
  }, []);

  useEffect(() => {
    if (selectedId) loadStrategy(selectedId);
  }, [selectedId]);

  async function loadStrategies() {
    try {
      const data = await api.strategy.list();
      setStrategies(data.strategies);
      if (!selectedId && data.strategies.length > 0) {
        setSelectedId(Number(data.strategies[0].id));
      }
    } catch {}
  }

  async function loadStrategy(id: number) {
    try {
      const data = await api.strategy.get(id);
      if (data.error) return;
      setCode(String(data.generated_code || ''));
      setName(String(data.name || ''));
      setTimeframe(String(data.timeframe || '15m'));
      setStoploss(Math.abs(Number(data.stoploss || 0.03)) * 100);
      setTakeProfit(Number(data.take_profit || 0.015) * 100);
      const lev = Number(data.default_leverage || 1);
      setLeverage(lev > 1 ? lev : 10);
      setFuturesEnabled(lev > 1);   // auto-tick if strategy already has leverage
      setPairs(Array.isArray(data.pairs) ? data.pairs.join(', ') : 'BTC/USDT');
    } catch {}
  }

  async function handleSave() {
    if (!selectedId) return;
    setSaving(true);
    try {
      await api.strategy.update(selectedId, {
        name,
        generated_code: code,
        timeframe,
        stoploss:          -(stoploss / 100),
        take_profit:       takeProfit / 100,
        default_leverage:  futuresEnabled ? leverage : 1,
        pairs: pairs.split(',').map((p) => p.trim()),
      });
      alert('Strategy saved!');
    } catch (e) {
      alert(`Error: ${e}`);
    }
    setSaving(false);
  }

  async function handleValidate() {
    setValidating(true);
    try {
      // Local AST-only validation — no AI round-trip, no API key required.
      const data = await api.strategy.validate({ code });
      setValidation(data as unknown as Record<string, unknown>);
    } catch (e) {
      setValidation({ valid: false, errors: [`Could not validate: ${e}`] });
    }
    setValidating(false);
  }

  async function handleAiAssist() {
    if (!aiPrompt.trim()) return;
    setAiLoading(true);
    try {
      const data = await api.strategy.aiAssist({ prompt: aiPrompt, existing_code: code });
      if (data.error) {
        alert(data.error);
      } else {
        setCode(String(data.code));
      }
    } catch (e) {
      alert(`Error: ${e}`);
    }
    setAiLoading(false);
    setAiPrompt('');
  }

  return (
    <div className="flex gap-6 h-[calc(100vh-8rem)]">
      {/* Editor */}
      <div className="flex-1 flex flex-col">
        <div className="flex items-center gap-4 mb-4">
          <select
            className="input max-w-xs"
            value={selectedId || ''}
            onChange={(e) => setSelectedId(Number(e.target.value))}
          >
            <option value="">Select strategy...</option>
            {strategies.map((s) => (
              <option key={String(s.id)} value={String(s.id)}>
                {String(s.name)}
              </option>
            ))}
          </select>
          <input className="input max-w-xs" value={name} onChange={(e) => setName(e.target.value)} placeholder="Strategy name" />
          <button
            onClick={async () => {
              try {
                const r = await api.strategy.dedupe();
                setMsg(`✅ Cleaned: removed ${r.deleted ?? 0} duplicate(s), kept ${r.kept ?? 0} unique strategies`);
                setTimeout(() => setMsg(''), 5000);
                loadStrategies();
              } catch (e) {
                setMsg(`❌ Error: ${e}`);
                setTimeout(() => setMsg(''), 5000);
              }
            }}
            className="px-3 py-2 text-xs rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-300 hover:bg-amber-500/20 transition"
            title="Remove duplicate user strategies (templates are never touched)"
          >
            🧹 Clean Duplicates
          </button>
          {selectedId && (
            <button
              onClick={async () => {
                if (!deleteConfirm) {
                  setDeleteConfirm(true);
                  setTimeout(() => setDeleteConfirm(false), 5000);
                  return;
                }
                try {
                  await api.strategy.delete(selectedId);
                  setSelectedId(null);
                  setDeleteConfirm(false);
                  loadStrategies();
                  setMsg('✅ Strategy deleted');
                  setTimeout(() => setMsg(''), 3000);
                } catch (e) {
                  setMsg(`❌ Error: ${e}`);
                  setTimeout(() => setMsg(''), 3000);
                }
              }}
              className={`px-3 py-2 text-xs rounded-lg border transition ${deleteConfirm ? 'bg-red-500/30 border-red-500 text-white animate-pulse' : 'bg-red-500/10 border-red-500/30 text-red-300 hover:bg-red-500/20'}`}
            >
              {deleteConfirm ? '⚠ Click again to confirm' : '🗑 Delete'}
            </button>
          )}
          {msg && (
            <span className="text-xs px-3 py-2 rounded-lg bg-slate-800/60 border border-slate-700 text-slate-200">
              {msg}
            </span>
          )}
        </div>

        <div className="flex-1 rounded-lg overflow-hidden border border-[#2a3a52]">
          <MonacoEditor
            height="100%"
            language="python"
            theme="vs-dark"
            value={code}
            onChange={(v) => setCode(v || '')}
            options={{
              minimap: { enabled: false },
              fontSize: 13,
              lineNumbers: 'on',
              scrollBeyondLastLine: false,
              automaticLayout: true,
            }}
          />
        </div>

        {/* AI Assist */}
        <div className="mt-4 flex gap-2">
          <input
            className="input flex-1"
            value={aiPrompt}
            onChange={(e) => setAiPrompt(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleAiAssist()}
            placeholder="AI Assist: e.g., 'Add RSI divergence to entry signal'"
            disabled={aiLoading}
          />
          <button onClick={handleAiAssist} disabled={aiLoading || !aiPrompt} className="btn-primary">
            {aiLoading ? 'Thinking...' : 'Ask AI'}
          </button>
        </div>
      </div>

      {/* Sidebar */}
      <div className="w-72 space-y-4">
        <div className="card">
          <h3 className="font-semibold mb-3">Parameters</h3>
          <div className="space-y-3">
            <div>
              <label className="label">Timeframe</label>
              <select className="input" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
                {['1m', '5m', '15m', '30m', '1h', '4h', '1d'].map((tf) => (
                  <option key={tf} value={tf}>{tf}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="label">Pairs (comma separated)</label>
              <input className="input" value={pairs} onChange={(e) => setPairs(e.target.value)} />
            </div>
            <div>
              <label className="label">Stop-Loss: {stoploss}%</label>
              <input type="range" min={0.5} max={10} step={0.5} value={stoploss}
                onChange={(e) => setStoploss(Number(e.target.value))}
                className="w-full accent-red-500" />
            </div>
            <div>
              <label className="label">Take-Profit: {takeProfit}%</label>
              <input type="range" min={0.1} max={10} step={0.1} value={takeProfit}
                onChange={(e) => setTakeProfit(Number(e.target.value))}
                className="w-full accent-emerald-500" />
            </div>

            {/* Futures toggle */}
            <div className={`p-3 rounded-xl border transition-all ${
              futuresEnabled
                ? 'border-blue-500/40 bg-blue-500/10'
                : 'border-[#2a3a52] bg-[#0a0f1c]'
            }`}>
              <label className="flex items-center gap-3 cursor-pointer mb-2">
                <input
                  type="checkbox"
                  checked={futuresEnabled}
                  onChange={e => setFuturesEnabled(e.target.checked)}
                  className="w-4 h-4 accent-blue-500"
                />
                <span className={`text-sm font-semibold ${futuresEnabled ? 'text-blue-300' : 'text-slate-400'}`}>
                  ⚡ Enable Futures Trading
                </span>
              </label>
              <p className="text-xs text-slate-500 mb-2">
                Tick to set leverage — auto-fills Futures Paper / Live / Backtest pages
              </p>
              {futuresEnabled && (
                <div>
                  <label className="label text-blue-300">Leverage: {leverage}x
                    <span className="text-orange-400 ml-2 text-[10px]">Liq ~{(100/leverage).toFixed(1)}%</span>
                  </label>
                  <input type="range" min={2} max={50} step={1} value={leverage}
                    onChange={(e) => setLeverage(Number(e.target.value))}
                    className="w-full accent-blue-500 mt-1" />
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="space-y-2">
          <button onClick={handleValidate} disabled={validating} className="btn-secondary w-full">
            {validating ? 'Validating...' : 'Validate'}
          </button>
          <button onClick={handleSave} disabled={saving || !selectedId} className="btn-primary w-full">
            {saving ? 'Saving...' : 'Save Strategy'}
          </button>
        </div>

        {validation && (
          <div className={`card text-sm ${(validation as Record<string, unknown>).valid ? 'border-emerald-500/30' : 'border-red-500/30'}`}>
            {validation.valid ? (
              <p className="text-emerald-400">Code is valid!</p>
            ) : (
              <div className="text-red-400">
                <p className="font-medium">Issues found:</p>
                <ul className="list-disc list-inside mt-1 text-xs">
                  {(validation.errors as string[])?.map((e: string, i: number) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function StrategyEditorPage() {
  return (
    <Suspense fallback={<LoadingSpinner text="Loading editor..." />}>
      <EditorContent />
    </Suspense>
  );
}
