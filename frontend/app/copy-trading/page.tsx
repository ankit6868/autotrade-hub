'use client';
import { useEffect, useState, Suspense } from 'react';
import { api } from '@/lib/api';

function CopyTradingInner() {
  const [mySignals, setMySignals] = useState<any[]>([]);
  const [myFollowers, setMyFollowers] = useState<any[]>([]);
  const [mySubs, setMySubs] = useState<any[]>([]);
  const [feed, setFeed] = useState<any[]>([]);
  const [masterInput, setMasterInput] = useState('');
  const [copyMode, setCopyMode] = useState('paper');
  const [maxLeverage, setMaxLeverage] = useState(10);
  const [loading, setLoading] = useState(true);
  const [subscribing, setSubscribing] = useState(false);
  const [msg, setMsg] = useState('');
  const [activeTab, setActiveTab] = useState<'master' | 'follow' | 'feed'>('follow');

  // My user_id is fetched from the auth endpoint so we can display the master ID
  const [myUserId, setMyUserId] = useState<string>('');
  const [isMaster, setIsMaster] = useState(false);

  async function refresh() {
    setLoading(true);
    try {
      const [signals, followers, subs, feedData, me] = await Promise.all([
        api.copy.mySignals(),
        api.copy.myFollowers(),
        api.copy.mySubscriptions(),
        api.copy.feed(),
        // Get the current user_id (Clerk sub) for the master id display
        fetch('/api/config/auth-status').then(r => r.ok ? r.json() : null).catch(() => null),
      ]);
      setMySignals(signals.signals || []);
      setMyFollowers(followers.followers || []);
      setMySubs(subs.subscriptions || []);
      setFeed(feedData.signals || []);
      if (me?.user_id) setMyUserId(me.user_id);
      // If user has any signals broadcast, they're already a master
      if ((signals.signals || []).length > 0) setIsMaster(true);
    } catch {}
    setLoading(false);
  }

  useEffect(() => { refresh(); }, []);

  function copyToClipboard(text: string) {
    navigator.clipboard?.writeText(text).then(
      () => { setMsg('✅ Copied to clipboard!'); setTimeout(() => setMsg(''), 2000); },
      () => { setMsg('❌ Failed to copy'); setTimeout(() => setMsg(''), 2000); }
    );
  }

  async function subscribe() {
    if (!masterInput.trim()) return;
    setSubscribing(true);
    try {
      const r = await api.copy.subscribe({ master_user_id: masterInput.trim(), copy_mode: copyMode, max_leverage: maxLeverage });
      if (r.error) setMsg(`❌ ${r.error}`);
      else { setMsg('✅ Subscribed! You will now auto-copy their trades.'); setMasterInput(''); refresh(); }
    } catch (e) { setMsg(`❌ ${String(e)}`); }
    setSubscribing(false);
    setTimeout(() => setMsg(''), 5000);
  }

  async function unsubscribe(masterId: string) {
    await api.copy.unsubscribe(masterId);
    refresh();
  }

  async function becomeMaster() {
    const r = await api.copy.becomeMaster();
    if (r?.user_id) setMyUserId(r.user_id);
    setIsMaster(true);
    setMsg('✅ Copy trading enabled! Share your Master ID below with followers.');
    setTimeout(() => setMsg(''), 5000);
  }

  return (
    <div className="max-w-5xl mx-auto">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="heading-xl">📡 Copy Trading</h1>
          <p className="text-slate-400 text-sm mt-1">Follow master traders or broadcast your own signals</p>
        </div>
      </div>

      {msg && (
        <div className={`mb-4 p-3 rounded-xl text-sm border ${msg.startsWith('✅') ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' : 'bg-red-500/10 border-red-500/30 text-red-300'}`}>
          {msg}
        </div>
      )}

      {/* Tabs */}
      <div className="flex gap-2 mb-6">
        {([['follow','👥 Follow Masters'], ['feed','📊 Signal Feed'], ['master','📡 Be a Master']] as const).map(([tab, label]) => (
          <button key={tab} onClick={() => setActiveTab(tab)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${activeTab === tab ? 'bg-brand-500/20 border border-brand-500/40 text-brand-400' : 'bg-[#1a2236] border border-[#2a3a52] text-slate-400 hover:text-white'}`}>
            {label}
          </button>
        ))}
      </div>

      {/* Follow Masters */}
      {activeTab === 'follow' && (
        <div className="space-y-4">
          {/* Subscribe form */}
          <div className="card">
            <h2 className="font-semibold mb-4">Follow a Master Trader</h2>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-4">
              <div className="md:col-span-1">
                <label className="label">Master User ID</label>
                <input className="input" value={masterInput} onChange={e => setMasterInput(e.target.value)}
                  placeholder="Enter master's user ID..." />
              </div>
              <div>
                <label className="label">Copy Mode</label>
                <select className="input" value={copyMode} onChange={e => setCopyMode(e.target.value)}>
                  <option value="paper">Paper (virtual)</option>
                  <option value="live">Live (real money)</option>
                </select>
              </div>
              <div>
                <label className="label">Max Leverage: {maxLeverage}x</label>
                <input type="range" min={1} max={20} value={maxLeverage} onChange={e => setMaxLeverage(Number(e.target.value))} className="w-full accent-blue-500 mt-2" />
              </div>
            </div>
            {copyMode === 'live' && (
              <div className="p-3 mb-4 rounded-lg bg-red-500/10 border border-red-500/20 text-red-300 text-xs">
                ⚠ Live copy mode will execute REAL trades with your funds. Only use with masters you trust.
              </div>
            )}
            <button onClick={subscribe} disabled={subscribing || !masterInput.trim()}
              className="btn-primary disabled:opacity-50">
              {subscribing ? 'Subscribing…' : '📡 Subscribe'}
            </button>
          </div>

          {/* Active subscriptions */}
          <div className="card">
            <h2 className="font-semibold mb-4">Your Subscriptions ({mySubs.length})</h2>
            {mySubs.length === 0 ? (
              <p className="text-slate-500 text-sm">No active subscriptions. Follow a master above.</p>
            ) : (
              <div className="space-y-3">
                {mySubs.map((s: any) => (
                  <div key={s.id} className="flex items-center justify-between p-3 rounded-xl bg-[#0a0f1c] border border-[#2a3a52]">
                    <div>
                      <p className="font-medium text-sm">{s.master_user_id.slice(0, 12)}...</p>
                      <p className="text-xs text-slate-400">Mode: {s.copy_mode} · Max {s.max_leverage}x lev · Copied: {s.total_copied} trades</p>
                    </div>
                    <div className="flex items-center gap-3">
                      <span className={`text-sm font-semibold ${(s.total_profit || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(s.total_profit || 0) >= 0 ? '+' : ''}{(s.total_profit || 0).toFixed(4)} USDT
                      </span>
                      <button onClick={() => unsubscribe(s.master_user_id)}
                        className="text-xs px-2 py-1 rounded bg-red-500/20 border border-red-500/30 text-red-400 hover:bg-red-500/30">
                        Unfollow
                      </button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Signal Feed */}
      {activeTab === 'feed' && (
        <div className="card">
          <h2 className="font-semibold mb-4">Live Signal Feed from Masters You Follow</h2>
          {feed.length === 0 ? (
            <p className="text-slate-500 text-sm">No signals yet. Follow masters to see their signals here.</p>
          ) : (
            <div className="space-y-2">
              {feed.map((s: any) => (
                <div key={s.id} className={`flex items-center justify-between p-3 rounded-xl border ${s.signal_type === 'entry' ? 'bg-emerald-500/5 border-emerald-500/20' : 'bg-slate-800/40 border-[#2a3a52]'}`}>
                  <div className="flex items-center gap-3">
                    <span className={`text-lg font-bold ${s.direction === 'long' ? 'text-emerald-400' : 'text-red-400'}`}>
                      {s.direction === 'long' ? '▲' : '▼'}
                    </span>
                    <div>
                      <p className="font-medium text-sm">{s.pair} <span className="text-slate-400 text-xs">by {s.master}</span></p>
                      <p className="text-xs text-slate-400">{s.signal_type} · {s.market_type} · {s.leverage}x · {s.strategy_name}</p>
                    </div>
                  </div>
                  <div className="text-right">
                    <p className="font-mono text-sm">{Number(s.entry_price).toFixed(2)}</p>
                    {s.profit_abs !== null && (
                      <p className={`text-xs font-semibold ${(s.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(s.profit_abs || 0) >= 0 ? '+' : ''}{(s.profit_abs || 0).toFixed(4)} USDT
                      </p>
                    )}
                    <p className="text-xs text-slate-500">{new Date(s.broadcasted_at).toLocaleTimeString()}</p>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Become Master */}
      {activeTab === 'master' && (
        <div className="space-y-4">
          <div className="card">
            <h2 className="font-semibold mb-4">Broadcast Your Trades</h2>
            <p className="text-slate-400 text-sm mb-4">
              Enable copy trading to let others follow your strategy. When your bot opens or closes a trade,
              it will be broadcast to all your followers who will auto-copy it.
            </p>

            {/* Show actual Master ID once enabled */}
            {isMaster && myUserId ? (
              <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30 mb-4">
                <p className="text-emerald-300 text-sm font-medium mb-2">📡 Copy Trading is ACTIVE</p>
                <p className="text-slate-300 text-sm mb-2">Your Master ID:</p>
                <div className="flex items-center gap-2 p-3 rounded-lg bg-[#0a0f1c] border border-emerald-500/40">
                  <code className="flex-1 text-emerald-400 text-sm font-mono break-all">{myUserId}</code>
                  <button
                    onClick={() => copyToClipboard(myUserId)}
                    className="text-xs px-3 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 border border-emerald-500/40 text-emerald-300 transition"
                  >
                    📋 Copy
                  </button>
                </div>
                <p className="text-slate-400 text-xs mt-2">Share this ID with followers — they paste it on the Follow Masters tab to auto-copy your trades.</p>
                <p className="text-slate-400 text-xs mt-1">Followers: {myFollowers.length} · Signals broadcast: {mySignals.length}</p>
              </div>
            ) : (
              <div className="p-4 rounded-xl bg-amber-500/5 border border-amber-500/30 mb-4">
                <p className="text-amber-300 text-sm font-medium mb-2">⚠ Copy Trading is DISABLED</p>
                <p className="text-slate-400 text-xs">Click below to enable. Once enabled, your trades will be broadcast to followers in real-time.</p>
              </div>
            )}

            {!isMaster && (
              <button onClick={becomeMaster} className="btn-primary">
                📡 Enable Copy Trading on My Account
              </button>
            )}
          </div>

          {/* My signals broadcast history */}
          {mySignals.length > 0 && (
            <div className="card">
              <h2 className="font-semibold mb-4">Your Broadcast History ({mySignals.length})</h2>
              <div className="space-y-2">
                {mySignals.slice(0, 20).map((s: any) => (
                  <div key={s.id} className="flex items-center justify-between p-3 rounded-xl bg-[#0a0f1c] border border-[#2a3a52]">
                    <div className="flex items-center gap-3">
                      <span className={s.direction === 'long' ? 'text-emerald-400' : 'text-red-400'}>
                        {s.direction === 'long' ? '▲' : '▼'}
                      </span>
                      <div>
                        <p className="text-sm font-medium">{s.pair} — {s.market_type} {s.leverage}x</p>
                        <p className="text-xs text-slate-400">{s.strategy_name} · {new Date(s.broadcasted_at).toLocaleString()}</p>
                      </div>
                    </div>
                    {s.profit_abs !== null && (
                      <span className={`text-sm font-semibold ${(s.profit_abs || 0) >= 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                        {(s.profit_abs || 0) >= 0 ? '+' : ''}{(s.profit_abs || 0).toFixed(4)} USDT
                      </span>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* My followers */}
          {myFollowers.length > 0 && (
            <div className="card">
              <h2 className="font-semibold mb-4">Your Followers ({myFollowers.length})</h2>
              <div className="space-y-2">
                {myFollowers.map((f: any, i: number) => (
                  <div key={i} className="flex items-center justify-between p-3 rounded-xl bg-[#0a0f1c] border border-[#2a3a52]">
                    <p className="text-sm">{f.follower_id.slice(0, 12)}... ({f.copy_mode})</p>
                    <p className="text-xs text-slate-400">{f.total_copied} trades copied</p>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export default function CopyTradingPage() {
  return <Suspense><CopyTradingInner /></Suspense>;
}
