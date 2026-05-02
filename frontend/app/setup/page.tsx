'use client';

import { useState } from 'react';
import { api } from '@/lib/api';
import LoadingSpinner from '@/components/ui/LoadingSpinner';

type Step = 1 | 2 | 3 | 4;

export default function SetupPage() {
  const [step, setStep] = useState<Step>(1);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<Record<string, unknown> | null>(null);

  // Form state
  const [kucoinKey, setKucoinKey] = useState('');
  const [kucoinSecret, setKucoinSecret] = useState('');
  const [kucoinPassphrase, setKucoinPassphrase] = useState('');
  const [openrouterKey, setOpenrouterKey] = useState('');
  const [preferredModel, setPreferredModel] = useState('nvidia/nemotron-3-super-120b-a12b:free');
  const [maxPositionPct, setMaxPositionPct] = useState(5);
  const [maxOpenTrades, setMaxOpenTrades] = useState(3);
  const [maxDailyDrawdown, setMaxDailyDrawdown] = useState(5);
  const [defaultStoploss, setDefaultStoploss] = useState(3);
  const [telegramToken, setTelegramToken] = useState('');
  const [telegramChatId, setTelegramChatId] = useState('');

  const [models, setModels] = useState<{ id: string; name: string }[]>([]);

  async function testKucoin() {
    setTestResult(null);
    setSaving(true);
    try {
      // Save first, then test
      await api.config.setup({
        kucoin_key: kucoinKey,
        kucoin_secret: kucoinSecret,
        kucoin_passphrase: kucoinPassphrase,
        openrouter_key: openrouterKey,
        preferred_model: preferredModel,
        max_position_pct: maxPositionPct,
        max_open_trades: maxOpenTrades,
        max_daily_drawdown_pct: maxDailyDrawdown,
        default_stoploss_pct: defaultStoploss,
      });
      const result = await api.config.testKucoin();
      setTestResult(result);
    } catch (e: unknown) {
      setTestResult({ connected: false, error: String(e) });
    }
    setSaving(false);
  }

  async function testOpenRouter() {
    setTestResult(null);
    setSaving(true);
    try {
      await api.config.setup({
        kucoin_key: kucoinKey,
        kucoin_secret: kucoinSecret,
        kucoin_passphrase: kucoinPassphrase,
        openrouter_key: openrouterKey,
        preferred_model: preferredModel,
        max_position_pct: maxPositionPct,
        max_open_trades: maxOpenTrades,
        max_daily_drawdown_pct: maxDailyDrawdown,
        default_stoploss_pct: defaultStoploss,
      });
      const result = await api.config.testOpenrouter();
      setTestResult(result);
      if (result.connected) {
        const modelData = await api.config.models();
        setModels(modelData.models);
      }
    } catch (e: unknown) {
      setTestResult({ connected: false, error: String(e) });
    }
    setSaving(false);
  }

  async function saveAll() {
    setSaving(true);
    try {
      await api.config.setup({
        kucoin_key: kucoinKey,
        kucoin_secret: kucoinSecret,
        kucoin_passphrase: kucoinPassphrase,
        openrouter_key: openrouterKey,
        preferred_model: preferredModel,
        max_position_pct: maxPositionPct,
        max_open_trades: maxOpenTrades,
        max_daily_drawdown_pct: maxDailyDrawdown,
        default_stoploss_pct: defaultStoploss,
        telegram_token: telegramToken,
        telegram_chat_id: telegramChatId,
      });
      window.location.href = '/';
    } catch (e: unknown) {
      alert(`Error: ${e}`);
      setSaving(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto">
      <h1 className="text-3xl font-bold mb-2">Setup Wizard</h1>
      <p className="text-slate-400 mb-8">Configure your API keys and trading preferences</p>

      {/* Step indicators */}
      <div className="flex gap-2 mb-8">
        {[1, 2, 3, 4].map((s) => (
          <button
            key={s}
            onClick={() => { setStep(s as Step); setTestResult(null); }}
            className={`flex-1 py-2 rounded-lg text-sm font-medium transition-colors ${
              step === s ? 'bg-brand-600 text-white' : 'bg-[#2a3a52] text-slate-400 hover:text-white'
            }`}
          >
            Step {s}
          </button>
        ))}
      </div>

      {/* Step 1: KuCoin */}
      {step === 1 && (
        <div className="card">
          <h2 className="text-xl font-semibold mb-1">KuCoin API Keys</h2>
          <p className="text-slate-400 text-sm mb-6">
            Free to generate at{' '}
            <a href="https://www.kucoin.com/account/api" target="_blank" className="text-brand-400 hover:underline">
              kucoin.com/account/api
            </a>
          </p>

          <div className="space-y-4">
            <div>
              <label className="label">API Key</label>
              <input className="input" type="password" value={kucoinKey} onChange={(e) => setKucoinKey(e.target.value)} placeholder="Your KuCoin API key" />
            </div>
            <div>
              <label className="label">API Secret</label>
              <input className="input" type="password" value={kucoinSecret} onChange={(e) => setKucoinSecret(e.target.value)} placeholder="Your KuCoin API secret" />
            </div>
            <div>
              <label className="label">Passphrase</label>
              <input className="input" type="password" value={kucoinPassphrase} onChange={(e) => setKucoinPassphrase(e.target.value)} placeholder="Your KuCoin API passphrase" />
            </div>

            <div className="flex gap-3">
              <button onClick={testKucoin} disabled={saving || !kucoinKey} className="btn-secondary">
                {saving ? 'Testing...' : 'Test Connection'}
              </button>
              <button onClick={() => { setStep(2); setTestResult(null); }} className="btn-primary">
                Next
              </button>
            </div>

            {testResult && (
              <div className={`p-4 rounded-lg text-sm ${testResult.connected ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/10 text-red-400 border border-red-500/30'}`}>
                {testResult.connected
                  ? `Connected! USDT Balance: ${Number(testResult.usdt_balance).toFixed(2)}`
                  : `Failed: ${testResult.error}`}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 2: OpenRouter */}
      {step === 2 && (
        <div className="card">
          <h2 className="text-xl font-semibold mb-1">OpenRouter API Key</h2>
          <div className="flex items-center gap-2 mb-6">
            <span className="bg-emerald-500/20 text-emerald-400 text-xs font-bold px-2 py-1 rounded">100% FREE</span>
            <p className="text-slate-400 text-sm">
              Get yours at{' '}
              <a href="https://openrouter.ai/keys" target="_blank" className="text-brand-400 hover:underline">
                openrouter.ai/keys
              </a>
              {' '}— no credit card needed
            </p>
          </div>

          <div className="space-y-4">
            <div>
              <label className="label">API Key</label>
              <input className="input" type="password" value={openrouterKey} onChange={(e) => setOpenrouterKey(e.target.value)} placeholder="sk-or-v1-..." />
            </div>

            <div>
              <label className="label">Preferred Model</label>
              <select className="input" value={preferredModel} onChange={(e) => setPreferredModel(e.target.value)}>
                <option value="nvidia/nemotron-3-super-120b-a12b:free">Nemotron 3 Super 120B ⭐ (Recommended)</option>
                <option value="openai/gpt-oss-120b:free">GPT-OSS 120B (OpenAI free, fast)</option>
                <option value="openai/gpt-oss-20b:free">GPT-OSS 20B (OpenAI free, fastest)</option>
                <option value="google/gemma-4-31b-it:free">Gemma 4 31B (Google)</option>
                <option value="qwen/qwen3-coder:free">Qwen3 Coder (Best for code, may queue)</option>
                <option value="meta-llama/llama-3.3-70b-instruct:free">Llama 3.3 70B</option>
                <option value="nousresearch/hermes-3-llama-3.1-405b:free">Hermes 3 405B (Powerful)</option>
                <option value="z-ai/glm-4.5-air:free">GLM-4.5 Air</option>
                {models.filter(m =>
                  !['nvidia/nemotron-3-super-120b-a12b:free','openai/gpt-oss-120b:free','openai/gpt-oss-20b:free',
                    'google/gemma-4-31b-it:free','qwen/qwen3-coder:free','meta-llama/llama-3.3-70b-instruct:free',
                    'nousresearch/hermes-3-llama-3.1-405b:free','z-ai/glm-4.5-air:free'].includes(m.id)
                ).map((m) => (
                  <option key={m.id} value={m.id}>{m.name}</option>
                ))}
              </select>
            </div>

            <div className="flex gap-3">
              <button onClick={testOpenRouter} disabled={saving || !openrouterKey} className="btn-secondary">
                {saving ? 'Testing...' : 'Test Connection'}
              </button>
              <button onClick={() => { setStep(3); setTestResult(null); }} className="btn-primary">
                Next
              </button>
            </div>

            {testResult && (
              <div className={`p-4 rounded-lg text-sm ${testResult.connected ? 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30' : 'bg-red-500/10 text-red-400 border border-red-500/30'}`}>
                {testResult.connected
                  ? `Connected! ${testResult.free_models} free models available`
                  : `Failed: ${testResult.error}`}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Step 3: Risk Preferences */}
      {step === 3 && (
        <div className="card">
          <h2 className="text-xl font-semibold mb-1">Risk Preferences</h2>
          <p className="text-slate-400 text-sm mb-6">Set your trading safety limits</p>

          <div className="space-y-6">
            <div>
              <label className="label">Max Position Size: {maxPositionPct}% of portfolio</label>
              <input type="range" min={1} max={20} value={maxPositionPct} onChange={(e) => setMaxPositionPct(Number(e.target.value))} className="w-full accent-brand-500" />
              <div className="flex justify-between text-xs text-slate-500"><span>1%</span><span>20%</span></div>
            </div>
            <div>
              <label className="label">Max Open Trades: {maxOpenTrades}</label>
              <input type="range" min={1} max={10} value={maxOpenTrades} onChange={(e) => setMaxOpenTrades(Number(e.target.value))} className="w-full accent-brand-500" />
              <div className="flex justify-between text-xs text-slate-500"><span>1</span><span>10</span></div>
            </div>
            <div>
              <label className="label">Max Daily Drawdown: {maxDailyDrawdown}%</label>
              <input type="range" min={1} max={15} value={maxDailyDrawdown} onChange={(e) => setMaxDailyDrawdown(Number(e.target.value))} className="w-full accent-brand-500" />
              <div className="flex justify-between text-xs text-slate-500"><span>1%</span><span>15%</span></div>
            </div>
            <div>
              <label className="label">Default Stop-Loss: {defaultStoploss}%</label>
              <input type="range" min={1} max={10} value={defaultStoploss} onChange={(e) => setDefaultStoploss(Number(e.target.value))} className="w-full accent-brand-500" />
              <div className="flex justify-between text-xs text-slate-500"><span>1%</span><span>10%</span></div>
            </div>

            <div className="flex gap-3">
              <button onClick={() => setStep(2)} className="btn-secondary">Back</button>
              <button onClick={() => setStep(4)} className="btn-primary">Next</button>
            </div>
          </div>
        </div>
      )}

      {/* Step 4: Telegram (optional) */}
      {step === 4 && (
        <div className="card">
          <h2 className="text-xl font-semibold mb-1">Telegram Notifications</h2>
          <p className="text-slate-400 text-sm mb-6">Optional — get trade alerts on your phone</p>

          <div className="space-y-4">
            <div>
              <label className="label">Bot Token</label>
              <input className="input" value={telegramToken} onChange={(e) => setTelegramToken(e.target.value)} placeholder="123456:ABC-DEF..." />
            </div>
            <div>
              <label className="label">Chat ID</label>
              <input className="input" value={telegramChatId} onChange={(e) => setTelegramChatId(e.target.value)} placeholder="Your Telegram chat ID" />
            </div>

            <div className="flex gap-3">
              <button onClick={() => setStep(3)} className="btn-secondary">Back</button>
              <button onClick={saveAll} disabled={saving} className="btn-secondary">Skip</button>
              <button onClick={saveAll} disabled={saving} className="btn-success">
                {saving ? 'Saving...' : 'Save Configuration'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
