'use client';

import { useState, useRef } from 'react';
import { useRouter } from 'next/navigation';
import { api } from '@/lib/api';
import LoadingSpinner from '@/components/ui/LoadingSpinner';

export default function StrategyUploadPage() {
  const router = useRouter();
  const fileRef = useRef<HTMLInputElement>(null);
  const [mode, setMode] = useState<'upload' | 'type' | 'template'>('upload');
  const [file, setFile] = useState<File | null>(null);
  const [text, setText] = useState('');
  const [name, setName] = useState('My Strategy');
  const [loading, setLoading] = useState(false);
  const [loadingStep, setLoadingStep] = useState('');
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [result, setResult] = useState<any | null>(null);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);

  async function handleParse() {
    setLoading(true);
    setError('');
    setResult(null);

    try {
      if (mode === 'upload' && file) {
        setLoadingStep('Reading your document...');
        const formData = new FormData();
        formData.append('file', file);
        formData.append('name', name);

        setLoadingStep('Reading document...');
        await new Promise((r) => setTimeout(r, 300));
        setLoadingStep('Sending to AI for parsing...');

        const data = await api.strategy.upload(formData);
        if (data.error) {
          setError(formatError(data.error));
        } else {
          setLoadingStep('Validating...');
          setResult(data);
        }
      } else if (mode === 'type' && text) {
        const formData = new FormData();
        formData.append('text', text);
        formData.append('name', name);

        setLoadingStep('Sending to AI for parsing...');
        const data = await api.strategy.upload(formData);
        if (data.error) {
          setError(formatError(data.error));
        } else {
          setLoadingStep('Validating...');
          setResult(data);
        }
      }
    } catch (e: unknown) {
      const msg = String(e);
      if (msg.includes('ROUTER_EXTERNAL_TARGET_ERROR') || msg.includes('fetch failed') || msg.includes('ECONNREFUSED')) {
        setError(
          'Cannot reach the backend server. This usually means:\n' +
          '1. The backend is not deployed or not running\n' +
          '2. The BACKEND_URL environment variable is not set in Vercel\n' +
          '3. The backend URL is not publicly accessible\n\n' +
          'You can use "Save Without AI" below to save your strategy description directly.'
        );
      } else {
        setError(formatError(msg));
      }
    }
    setLoading(false);
    setLoadingStep('');
  }

  // Save the strategy text directly without AI parsing
  async function handleSaveWithoutAI() {
    setSaving(true);
    setError('');
    try {
      const strategyText = mode === 'upload' && file
        ? await file.text()
        : text;

      if (!strategyText.trim()) {
        setError('No strategy text to save');
        setSaving(false);
        return;
      }

      const formData = new FormData();
      if (mode === 'upload' && file) {
        formData.append('file', file);
      } else {
        formData.append('text', strategyText);
      }
      formData.append('name', name);
      formData.append('skip_ai', 'true');

      const data = await api.strategy.upload(formData);
      if (data.error) {
        // If backend is unreachable, show a clear message
        if (String(data.error).includes('ROUTER_EXTERNAL_TARGET_ERROR') || String(data.error).includes('fetch failed')) {
          setError('Backend server is unreachable. Please check your deployment configuration.');
        } else {
          setError(formatError(data.error));
        }
      } else {
        setResult(data);
        setSaved(true);
      }
    } catch (e: unknown) {
      const msg = String(e);
      if (msg.includes('ROUTER_EXTERNAL_TARGET_ERROR') || msg.includes('fetch failed') || msg.includes('ECONNREFUSED')) {
        setError('Backend server is unreachable. Please check your deployment configuration (BACKEND_URL in Vercel).');
      } else {
        setError(formatError(msg));
      }
    }
    setSaving(false);
  }

  async function handleSave() {
    if (!result?.id) return;
    setSaving(true);
    try {
      await api.strategy.update(result.id, { name });
      setSaved(true);
    } catch {
      // Strategy already saved from parsing
      setSaved(true);
    }
    setSaving(false);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    const dropped = e.dataTransfer.files[0];
    if (dropped) setFile(dropped);
  }

  function formatError(msg: string): string {
    if (msg.includes('ROUTER_EXTERNAL_TARGET_ERROR')) {
      return 'Cannot reach the backend server. Check that your backend is deployed and BACKEND_URL is set correctly in Vercel.';
    }
    if (msg.includes('OpenRouter key not configured')) {
      return 'OpenRouter API key not configured. Go to Setup page to add your free OpenRouter API key for AI strategy parsing.';
    }
    if (msg.includes('decrypted')) {
      return 'Your OpenRouter API key needs to be re-entered. Go to Setup and save your key again.';
    }
    return msg;
  }

  return (
    <div className="max-w-4xl mx-auto">
      <h1 className="heading-xl mb-2">Upload Strategy</h1>
      <p className="text-slate-400 mb-6 sm:mb-8 text-sm sm:text-base">Upload your trading strategy and let AI convert it to code</p>

      {/* Mode selector */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4 mb-6 sm:mb-8">
        {[
          { key: 'upload', label: 'Upload Document', desc: 'PDF, DOCX, TXT, MD', icon: '📄' },
          { key: 'type', label: 'Type It Out', desc: 'Write your rules directly', icon: '✍️' },
          { key: 'template', label: 'Use a Template', desc: '4 pre-built strategies', icon: '📋' },
        ].map((m) => (
          <button
            key={m.key}
            onClick={() => { setMode(m.key as typeof mode); setResult(null); setError(''); }}
            className={`card text-left transition-all ${
              mode === m.key ? 'border-brand-500 ring-1 ring-brand-500' : 'hover:border-slate-500'
            }`}
          >
            <span className="text-2xl">{m.icon}</span>
            <h3 className="font-semibold mt-2">{m.label}</h3>
            <p className="text-sm text-slate-400">{m.desc}</p>
          </button>
        ))}
      </div>

      {/* Name input */}
      <div className="mb-6">
        <label className="label">Strategy Name</label>
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="My Strategy" />
      </div>

      {/* Upload mode */}
      {mode === 'upload' && (
        <div
          onDrop={handleDrop}
          onDragOver={(e) => e.preventDefault()}
          onClick={() => fileRef.current?.click()}
          className="card border-dashed border-2 cursor-pointer hover:border-brand-500 transition-colors text-center py-12 mb-6"
        >
          <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" className="hidden" onChange={(e) => setFile(e.target.files?.[0] || null)} />
          {file ? (
            <div>
              <p className="text-lg font-medium">{file.name}</p>
              <p className="text-sm text-slate-400 mt-1">{(file.size / 1024).toFixed(1)} KB</p>
            </div>
          ) : (
            <div>
              <p className="text-lg text-slate-400">Drop your file here or click to browse</p>
              <p className="text-sm text-slate-500 mt-1">PDF, DOCX, TXT, or MD</p>
            </div>
          )}
        </div>
      )}

      {/* Type mode */}
      {mode === 'type' && (
        <div className="mb-6">
          <label className="label">Describe your trading strategy</label>
          <textarea
            className="input min-h-[200px] font-mono text-sm"
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={`Example:\n\nBuy when:\n- RSI(14) drops below 30\n- Price is below the lower Bollinger Band (20, 2)\n- Volume is above 1.5x 20-period average\n\nSell when:\n- RSI goes above 70\n- OR price hits upper Bollinger Band\n\nStop-loss: 3%\nTimeframe: 15 minutes\nPairs: BTC/USDT, ETH/USDT`}
          />
        </div>
      )}

      {/* Template mode */}
      {mode === 'template' && (
        <div className="mb-6">
          <p className="text-slate-400 mb-4">Pre-built strategies — no AI needed</p>
          <button onClick={() => router.push('/strategy/templates')} className="btn-primary">
            Browse Templates
          </button>
        </div>
      )}

      {/* Action buttons */}
      {mode !== 'template' && (
        <div className="flex flex-wrap gap-3 mb-8">
          <button
            onClick={handleParse}
            disabled={loading || saving || (mode === 'upload' && !file) || (mode === 'type' && !text)}
            className="btn-primary"
          >
            {loading ? 'Parsing...' : 'Parse Strategy with AI'}
          </button>
          <button
            onClick={handleSaveWithoutAI}
            disabled={loading || saving || (mode === 'upload' && !file) || (mode === 'type' && !text)}
            className="px-6 py-2.5 rounded-xl font-semibold text-sm border border-white/10 text-slate-300 hover:bg-white/5 transition-all disabled:opacity-50"
          >
            {saving ? 'Saving...' : 'Save Without AI'}
          </button>
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="card mb-8">
          <LoadingSpinner text={loadingStep} />
          <p className="text-center text-xs text-slate-500 mt-2">Free models can take 30-60 seconds. Please wait...</p>
        </div>
      )}

      {/* Error */}
      {error && (
        <div className="card mb-8 border-red-500/30 bg-red-500/10">
          <p className="text-red-400 whitespace-pre-line">{error}</p>
          {error.includes('backend') && (
            <div className="mt-3 pt-3 border-t border-red-500/20">
              <p className="text-slate-400 text-sm mb-2">Quick fixes:</p>
              <ul className="text-sm text-slate-500 list-disc list-inside space-y-1">
                <li>Check that your backend is deployed and accessible</li>
                <li>In Vercel project settings, add <code className="text-slate-300 bg-slate-800 px-1 rounded">BACKEND_URL</code> pointing to your backend</li>
                <li>Use &quot;Save Without AI&quot; to save the strategy description directly</li>
              </ul>
            </div>
          )}
          {error.includes('OpenRouter') && (
            <div className="mt-3 pt-3 border-t border-red-500/20">
              <button
                onClick={() => router.push('/setup')}
                className="text-sm text-emerald-400 hover:text-emerald-300 underline"
              >
                Go to Setup to configure OpenRouter API key
              </button>
            </div>
          )}
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold">
              {result.model_used ? 'Generated Strategy' : 'Strategy Saved'}
            </h2>
            {result.model_used && (
              <span className="text-xs text-slate-500">Model: {String(result.model_used)}</span>
            )}
          </div>

          {/* Save success banner */}
          {saved && (
            <div className="mb-4 p-3 rounded-xl bg-emerald-500/15 border border-emerald-500/40 text-emerald-300 text-sm flex items-center gap-2">
              <span>&#9989;</span> <strong>Strategy saved!</strong> You can find it in{' '}
              <button
                onClick={() => router.push('/strategy/editor?id=' + result.id)}
                className="underline hover:text-emerald-200"
              >
                Strategy Editor
              </button>
              {' '}and in the <button
                onClick={() => router.push('/futures-trade')}
                className="underline hover:text-emerald-200"
              >
                Futures Terminal Bot Panel
              </button>
            </div>
          )}

          {result.validation && !(result.validation as Record<string, unknown>).valid && (
            <div className="mb-4 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30 text-yellow-400 text-sm">
              <p className="font-medium">Validation warnings:</p>
              <ul className="list-disc list-inside mt-1">
                {((result.validation as Record<string, unknown>).errors as string[]).map((e: string, i: number) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </div>
          )}

          <pre className="bg-[#0a0f1c] rounded-lg p-4 overflow-x-auto text-sm font-mono text-slate-300 max-h-[500px] overflow-y-auto mb-4">
            {String(result.code)}
          </pre>

          <div className="flex flex-wrap gap-3">
            {/* Primary: Save Strategy */}
            {!saved ? (
              <button
                onClick={handleSave}
                disabled={saving}
                className="px-6 py-2.5 rounded-xl font-semibold text-sm bg-emerald-500/20 border border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/30 transition-all disabled:opacity-50"
              >
                {saving ? 'Saving...' : 'Save Strategy'}
              </button>
            ) : (
              <button
                onClick={() => router.push('/strategy/editor?id=' + result.id)}
                className="px-6 py-2.5 rounded-xl font-semibold text-sm bg-emerald-500/20 border border-emerald-500/50 text-emerald-300 hover:bg-emerald-500/30 transition-all"
              >
                Open in Editor
              </button>
            )}
            <button
              onClick={() => router.push(`/strategy/editor?id=${result.id}`)}
              className="btn-primary"
            >
              Edit Code
            </button>
            <button onClick={() => router.push('/futures-trade')} className="btn-secondary">
              Use in Futures Terminal
            </button>
            <button onClick={() => router.push('/backtest')} className="btn-secondary">
              Run Backtest
            </button>
            <button onClick={() => { setResult(null); setError(''); setSaved(false); }} className="btn-secondary">
              Re-generate
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
