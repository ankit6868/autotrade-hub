'use client';
import { useState } from 'react';
import { api } from '@/lib/api';

/**
 * Self-contained card for the NORMAL KuCoin Futures API keys (separate from the
 * Lead copy-trading keys). Powers the /regular-futures-trade terminal. Additive
 * — it doesn't touch the existing setup wizard.
 */
export default function RegularFuturesKeys() {
  const [key, setKey] = useState('');
  const [secret, setSecret] = useState('');
  const [pass, setPass] = useState('');
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);

  async function save(test: boolean) {
    setBusy(true);
    setSaved(false);
    setResult(null);
    try {
      await api.config.setupRegular({
        kucoin_reg_key: key,
        kucoin_reg_secret: secret,
        kucoin_reg_passphrase: pass,
      });
      setSaved(true);
      if (test) setResult(await api.config.testKucoinRegular());
    } catch (e) {
      setResult({ connected: false, error: String(e) });
    }
    setBusy(false);
  }

  return (
    <div className="card mt-6">
      <h2 className="text-xl font-semibold mb-1">Regular Futures API Keys</h2>
      <p className="text-slate-400 text-sm mb-6">
        Optional — a normal KuCoin Futures API key (General + Trade permissions, no Withdraw) for the{' '}
        <a href="/regular-futures-trade" className="text-brand-400 hover:underline">Regular Futures terminal</a>.
        Kept separate from your Lead copy-trading keys.
      </p>

      <div className="space-y-4">
        <div>
          <label className="label">API Key</label>
          <input className="input" type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder="Regular futures API key" />
        </div>
        <div>
          <label className="label">API Secret</label>
          <input className="input" type="password" value={secret} onChange={(e) => setSecret(e.target.value)} placeholder="Regular futures API secret" />
        </div>
        <div>
          <label className="label">Passphrase</label>
          <input className="input" type="password" value={pass} onChange={(e) => setPass(e.target.value)} placeholder="Regular futures API passphrase" />
        </div>

        <div className="flex gap-3">
          <button onClick={() => save(true)} disabled={busy || !key} className="btn-secondary">
            {busy ? 'Working…' : 'Save & Test'}
          </button>
          <button onClick={() => save(false)} disabled={busy || !key} className="btn-primary">
            {busy ? 'Saving…' : 'Save'}
          </button>
        </div>

        {saved && !result && (
          <div className="p-3 rounded-lg text-sm bg-emerald-500/10 text-emerald-400 border border-emerald-500/30">
            Regular futures keys saved.
          </div>
        )}
        {result && (
          <div className={`p-4 rounded-lg text-sm ${result.connected ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/10 text-red-400 border border-red-500/30'}`}>
            {result.connected
              ? `Connected — available $${Number(result.available_balance || 0).toFixed(2)} USDT (equity $${Number(result.usdt_balance || 0).toFixed(2)}).`
              : `Failed: ${String(result.error || 'unknown error')}`}
          </div>
        )}
      </div>
    </div>
  );
}
