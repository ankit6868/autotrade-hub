'use client';

import { useCallback, useEffect, useState } from 'react';
import { useClerk } from '@clerk/nextjs';
import { api } from '@/lib/api';
import { useVisibleInterval } from '@/lib/useVisibleInterval';

type Status = {
  active: boolean;
  tier: string;
  is_admin: boolean;
  kind: string | null;
  expires_at: string | null;
  expired?: boolean;
  paused?: boolean;
  email_seen?: boolean;
} | null;

/**
 * Gates the app behind an access code AFTER Clerk login.
 *   • admin / unlimited-allowlist emails  → pass straight through (no code)
 *   • users with an active code           → pass
 *   • everyone else                       → the code-entry screen
 * The binding lives server-side, so this never re-asks after logout/login
 * once a code is redeemed (or the email is allowlisted).
 */
export default function AccessGate({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<Status>(null);
  const [loading, setLoading] = useState(true);
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const { signOut } = useClerk();

  const check = useCallback(async () => {
    try {
      const s = await api.access.status();
      setStatus(s as Status);
    } catch {
      // If the status call itself fails (network/transient), fail OPEN so a
      // glitch doesn't lock a paying user out — the backend still enforces
      // on the trading endpoints.
      setStatus({ active: true, tier: 'error_fail_open', is_admin: false, kind: null, expires_at: null });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { check(); }, [check]);
  // Live re-gate: re-check every 60s while the tab is visible, and immediately
  // when the tab regains visibility, so an expiring subscription drops the user
  // back to the code screen without a manual reload. Hidden tabs don't poll.
  // (The backend also 402s their API calls immediately.)
  useVisibleInterval(check, 60_000);

  const redeem = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!code.trim()) return;
    setSubmitting(true); setError('');
    try {
      const r: any = await api.access.redeem(code.trim());
      if (r?.ok) { setCode(''); await check(); }
      else setError(r?.error || 'Could not redeem this code.');
    } catch {
      setError('Network error — please try again.');
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="min-h-screen w-full flex items-center justify-center text-slate-400 text-sm">Checking access…</div>;
  }
  if (status?.active) return <>{children}</>;

  // ── Code-entry gate ──
  const ended = status?.expired;            // had access before, now lapsed/paused
  const paused = status?.paused;
  return (
    <div className="min-h-screen w-full flex items-center justify-center p-6">
      <div className="max-w-sm w-full space-y-5 p-7 bg-[#111827] rounded-2xl border border-[#2a3a52]">
        <div className="text-center space-y-1.5">
          <h1 className="text-xl font-bold text-white">
            {ended ? (paused ? 'Access paused' : 'Subscription finished') : 'Enter your access code'}
          </h1>
          <p className="text-xs text-slate-400">
            {ended
              ? (paused
                  ? 'Your access has been paused by the admin. Enter a new code, or contact the admin to resume.'
                  : 'Your subscription period has ended. Enter a new code to continue using AutoTrade Hub.')
              : 'AutoTrade Hub is invite-only. Enter the code you were given to unlock the app.'}
          </p>
        </div>
        <form onSubmit={redeem} className="space-y-3">
          <input
            value={code}
            onChange={(e) => setCode(e.target.value.toUpperCase())}
            placeholder="ATH-XXXX-XXXX"
            autoFocus
            className="w-full px-3 py-2.5 rounded-lg bg-[#0f1729] border border-[#2a3a52] text-center tracking-widest text-slate-100 placeholder:text-slate-600 focus:border-emerald-500/50 outline-none"
          />
          {error && <p className="text-xs text-rose-400 text-center">{error}</p>}
          <button type="submit" disabled={submitting || !code.trim()} className="btn-primary w-full disabled:opacity-50">
            {submitting ? 'Checking…' : 'Unlock'}
          </button>
        </form>
        <div className="text-center">
          <button
            onClick={() => signOut({ redirectUrl: '/sign-in' })}
            className="text-[11px] text-slate-500 hover:text-slate-300 underline underline-offset-2"
          >
            Sign in with a different account
          </button>
        </div>
      </div>
    </div>
  );
}
