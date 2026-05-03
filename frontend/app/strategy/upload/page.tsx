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
        setLoadingStep('Sending to AI (Nemotron Super 120B)...');

        const data = await api.strategy.upload(formData);
        if (data.error) {
          setError(String(data.error));
        } else {
          setLoadingStep('Validating...');
          setResult(data);
        }
      } else if (mode === 'type' && text) {
        const formData = new FormData();
        formData.append('text', text);
        formData.append('name', name);

        setLoadingStep('Sending to AI (Nemotron Super 120B)...');
        const data = await api.strategy.upload(formData);
        if (data.error) {
          // Show the full error message — it includes which models were tried
          setError(String(data.error));
        } else {
          setLoadingStep('Validating...');
          setResult(data);
        }
      }
    } catch (e: unknown) {
      setError(String(e));
    }
    setLoading(false);
    setLoadingStep('');
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    const dropped = e.dataTransfer.files[0];
    if (dropped) setFile(dropped);
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

      {/* Parse button */}
      {mode !== 'template' && (
        <button
          onClick={handleParse}
          disabled={loading || (mode === 'upload' && !file) || (mode === 'type' && !text)}
          className="btn-primary mb-8"
        >
          {loading ? 'Parsing...' : 'Parse Strategy with AI'}
        </button>
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
          <p className="text-red-400">{error}</p>
        </div>
      )}

      {/* Result */}
      {result && (
        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-xl font-semibold">Generated Strategy</h2>
            <span className="text-xs text-slate-500">Model: {String(result.model_used)}</span>
          </div>

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

          <div className="flex gap-3">
            <button
              onClick={() => router.push(`/strategy/editor?id=${result.id}`)}
              className="btn-primary"
            >
              Edit Code
            </button>
            <button onClick={() => router.push('/backtest')} className="btn-secondary">
              Run Backtest
            </button>
            <button onClick={() => { setResult(null); setError(''); }} className="btn-secondary">
              Re-generate
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
