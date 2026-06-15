'use client';

import { useCallback, useEffect, useState } from 'react';
import { api } from '@/lib/api';

/** Countdown string from now → an ISO timestamp. */
function remaining(expISO: string | null): string {
  if (!expISO) return '';
  const ms = new Date(expISO).getTime() - Date.now();
  if (ms <= 0) return 'expired';
  const s = Math.floor(ms / 1000);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60), sec = s % 60;
  return `${d}d ${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`;
}

export default function AccessPanel() {
  const [st, setSt] = useState<any>(null);
  const [, setTick] = useState(0);
  const [code, setCode] = useState('');
  const [msg, setMsg] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try { setSt(await api.access.status()); } catch { /* ignore */ }
  }, []);
  useEffect(() => { refresh(); }, [refresh]);
  // tick the countdown every second (subscription only)
  useEffect(() => {
    if (st?.kind !== 'subscription' || !st?.expires_at) return;
    const id = setInterval(() => setTick((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [st]);

  if (!st) return null;

  const changeCode = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return;
    setBusy(true); setMsg('');
    try {
      const r: any = await api.access.redeem(code.trim());
      if (r?.ok) { setCode(''); setMsg('✓ Code applied.'); await refresh(); }
      else setMsg(r?.error || 'Could not apply code.');
    } catch { setMsg('Network error.'); }
    finally { setBusy(false); }
  };

  const tier: string = st.tier;
  const isSub = st.kind === 'subscription';
  const lifetime = st.kind === 'unlimited' || tier === 'admin' || tier === 'unlimited';

  return (
    <section className="rounded-2xl bg-[#0f1830] border border-white/[0.06] p-5 space-y-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <h2 className="text-sm font-bold text-white">Your access</h2>
        <span className={`text-[10px] font-semibold px-2 py-0.5 rounded-full ${
          tier === 'admin' ? 'bg-amber-500/20 text-amber-300'
          : lifetime ? 'bg-emerald-500/20 text-emerald-300'
          : 'bg-sky-500/20 text-sky-300'}`}>
          {tier === 'admin' ? 'ADMIN' : lifetime ? 'LIFETIME' : 'SUBSCRIPTION'}
        </span>
      </div>

      {/* Subscription → live countdown + change/upgrade */}
      {isSub && (
        <div className="space-y-3">
          <div>
            <div className="text-[11px] text-slate-400">Time remaining</div>
            <div className={`text-lg font-mono ${remaining(st.expires_at) === 'expired' ? 'text-rose-400' : 'text-emerald-300'}`}>
              {remaining(st.expires_at) || '—'}
            </div>
          </div>
          <form onSubmit={changeCode} className="flex gap-2">
            <input
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="Enter a new code (renew or upgrade)"
              className="flex-1 px-3 py-2 rounded-lg bg-[#0f1729] border border-[#2a3a52] text-xs text-slate-100 placeholder:text-slate-600 outline-none focus:border-emerald-500/50"
            />
            <button disabled={busy || !code.trim()} className="btn-primary text-xs px-3 disabled:opacity-50">
              {busy ? '…' : 'Apply'}
            </button>
          </form>
          <p className="text-[10px] text-slate-500">
            Enter another subscription code to <b className="text-slate-300">renew</b>, or an unlimited code to <b className="text-slate-300">upgrade to lifetime</b>.
          </p>
          {msg && <p className="text-[11px] text-slate-300">{msg}</p>}
        </div>
      )}

      {lifetime && tier !== 'admin' && (
        <p className="text-xs text-slate-400">Lifetime access — no renewal needed.</p>
      )}

      {tier === 'admin' && <AdminCodes />}
      {tier === 'admin' && <AdminUsers />}
    </section>
  );
}

/** Admin-only: generate + view/revoke codes. */
function AdminCodes() {
  const [kind, setKind] = useState<'subscription' | 'unlimited'>('subscription');
  const [count, setCount] = useState(9);
  const [generated, setGenerated] = useState<string[]>([]);
  const [codes, setCodes] = useState<any[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const loadList = async () => {
    try { const r = await api.access.listCodes(); setCodes(r.codes || []); } catch { setErr('Could not load codes.'); }
  };

  const generate = async () => {
    setBusy(true); setErr(''); setGenerated([]);
    try {
      const r: any = await api.access.generate(kind, count);
      setGenerated(r.codes || []);
      await loadList();
    } catch { setErr('Generation failed.'); }
    finally { setBusy(false); }
  };

  const revoke = async (c: string, revoked: boolean) => {
    await api.access.revoke(c, revoked); await loadList();
  };

  return (
    <div className="border-t border-white/[0.06] pt-4 space-y-3">
      <div className="text-xs font-bold text-amber-300">Admin · generate codes</div>
      <div className="flex items-end gap-2 flex-wrap">
        <label className="text-[11px] text-slate-400">
          Type
          <select value={kind} onChange={(e) => setKind(e.target.value as any)}
            className="block mt-1 px-2 py-1.5 rounded bg-[#0f1729] border border-[#2a3a52] text-xs text-slate-100">
            <option value="subscription">Subscription (30 days)</option>
            <option value="unlimited">Unlimited (lifetime)</option>
          </select>
        </label>
        <label className="text-[11px] text-slate-400">
          Count
          <input type="number" min={1} max={500} value={count}
            onChange={(e) => setCount(Math.max(1, Math.min(500, Number(e.target.value) || 1)))}
            className="block mt-1 w-20 px-2 py-1.5 rounded bg-[#0f1729] border border-[#2a3a52] text-xs text-slate-100" />
        </label>
        <button onClick={generate} disabled={busy} className="btn-primary text-xs px-3 disabled:opacity-50">
          {busy ? '…' : 'Generate'}
        </button>
        <button onClick={loadList} className="text-xs px-3 py-1.5 rounded border border-[#2a3a52] text-slate-300 hover:bg-white/5">
          View all
        </button>
      </div>
      {err && <p className="text-[11px] text-rose-400">{err}</p>}

      {generated.length > 0 && (
        <div className="rounded-lg bg-[#0f1729] border border-emerald-500/20 p-3">
          <div className="text-[11px] text-emerald-300 mb-1">New codes — copy &amp; hand out:</div>
          <pre className="text-xs text-slate-200 whitespace-pre-wrap break-all">{generated.join('\n')}</pre>
        </div>
      )}

      {codes && (
        <div className="max-h-64 overflow-auto rounded-lg border border-[#2a3a52]">
          <table className="w-full text-[11px]">
            <thead className="text-slate-500 sticky top-0 bg-[#0f1830]">
              <tr><th className="text-left p-2">Code</th><th className="p-2">Kind</th><th className="p-2">Status</th><th className="p-2">Bound</th><th className="p-2"></th></tr>
            </thead>
            <tbody>
              {codes.map((c) => (
                <tr key={c.code} className="border-t border-white/[0.04]">
                  <td className="p-2 font-mono text-slate-200">{c.code}</td>
                  <td className="p-2 text-center text-slate-400">{c.kind === 'unlimited' ? '∞' : '30d'}</td>
                  <td className="p-2 text-center">{c.status}</td>
                  <td className="p-2 text-center text-slate-400">{c.bound_email || '—'}</td>
                  <td className="p-2 text-center">
                    <button onClick={() => revoke(c.code, !c.revoked)}
                      className="text-[10px] underline text-slate-400 hover:text-slate-200">
                      {c.revoked ? 'unrevoke' : 'revoke'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/** Admin-only: view code-redeeming users, extend their subscription, change
 *  their code. */
function AdminUsers() {
  const [users, setUsers] = useState<any[] | null>(null);
  const [allowlist, setAllowlist] = useState<any[]>([]);
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');

  const load = useCallback(async () => {
    try {
      const r = await api.access.listUsers();
      setUsers(r.users || []); setAllowlist(r.allowlist || []);
    } catch { setNote('Could not load users.'); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const extend = async (uid: string, days: number, months: number) => {
    setBusy(uid); setNote('');
    try {
      const r: any = await api.access.extendUser(uid, days, months);
      setNote(r?.ok ? `✓ Extended → ${r.expires_at}` : (r?.error || 'Extend failed.'));
      await load();
    } finally { setBusy(''); }
  };
  const changeCode = async (uid: string) => {
    if (!confirm('Issue a NEW code to this user and revoke their old one?')) return;
    setBusy(uid); setNote('');
    try {
      const r: any = await api.access.changeUserCode(uid);
      setNote(r?.ok ? `✓ New code for user: ${r.new_code}` : (r?.error || 'Change failed.'));
      await load();
    } finally { setBusy(''); }
  };

  return (
    <div className="border-t border-white/[0.06] pt-4 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-bold text-amber-300">Admin · users</div>
        <button onClick={load} className="text-[11px] underline text-slate-400 hover:text-slate-200">refresh</button>
      </div>
      {note && <p className="text-[11px] text-slate-300">{note}</p>}

      {users && users.length > 0 ? (
        <div className="max-h-72 overflow-auto rounded-lg border border-[#2a3a52]">
          <table className="w-full text-[11px]">
            <thead className="text-slate-500 sticky top-0 bg-[#0f1830]">
              <tr>
                <th className="text-left p-2">User</th><th className="p-2">Plan</th>
                <th className="p-2">Expires</th><th className="p-2">Manage</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => (
                <tr key={u.user_id} className="border-t border-white/[0.04] align-top">
                  <td className="p-2 text-slate-200">
                    {u.email || u.user_id}
                    <div className="text-[9px] text-slate-500 font-mono">{u.current_code}</div>
                  </td>
                  <td className="p-2 text-center">
                    <span className={u.kind === 'unlimited' ? 'text-emerald-300' : (u.active ? 'text-sky-300' : 'text-rose-400')}>
                      {u.kind === 'unlimited' ? 'Lifetime' : (u.active ? 'Sub' : 'Expired')}
                    </span>
                  </td>
                  <td className="p-2 text-center text-slate-400">
                    {u.kind === 'unlimited' ? '—' : (u.expires_at ? u.expires_at.replace('T', ' ').replace('Z', '') : '—')}
                  </td>
                  <td className="p-2">
                    {u.kind !== 'unlimited' && (
                      <div className="flex flex-wrap gap-1 justify-center">
                        <button disabled={busy === u.user_id} onClick={() => extend(u.user_id, 30, 0)} className="px-1.5 py-0.5 rounded border border-[#2a3a52] hover:bg-white/5">+30d</button>
                        <button disabled={busy === u.user_id} onClick={() => extend(u.user_id, 0, 3)} className="px-1.5 py-0.5 rounded border border-[#2a3a52] hover:bg-white/5">+3mo</button>
                        <button disabled={busy === u.user_id} onClick={() => extend(u.user_id, 365, 0)} className="px-1.5 py-0.5 rounded border border-[#2a3a52] hover:bg-white/5">+1yr</button>
                      </div>
                    )}
                    <div className="flex justify-center mt-1">
                      <button disabled={busy === u.user_id} onClick={() => changeCode(u.user_id)} className="px-1.5 py-0.5 rounded border border-amber-500/30 text-amber-300 hover:bg-amber-500/10">change code</button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-[11px] text-slate-500">No code-redeeming users yet.</p>
      )}

      {allowlist.length > 0 && (
        <div className="text-[10px] text-slate-500">
          <span className="text-slate-400">Allowlist (env, lifetime, read-only): </span>
          {allowlist.map((a) => `${a.email} (${a.tier})`).join(', ')}
        </div>
      )}
    </div>
  );
}
