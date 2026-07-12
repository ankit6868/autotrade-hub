'use client';
// Regular Futures Terminal — identical to /futures-trade but wired to the user's
// NORMAL KuCoin Futures API (private account) instead of Lead copy-trading.
//
// It reuses the exact same terminal component; the ONLY difference is that this
// wrapper flips the api client into 'regular' mode, so every /api/futures/*
// request carries `X-Futures-Api: regular` and the backend routes to the
// regular order endpoints (/api/v1/orders + /st-orders) using the user's
// regular-futures API keys. The Lead terminal is completely untouched.
import { useEffect } from 'react';
import { setFuturesApiMode } from '@/lib/api';
import FuturesTerminal from '../futures-trade/page';

export default function RegularFuturesTerminalPage() {
  // Set synchronously so the terminal's first data fetches (fired from its own
  // mount effects, which run after this component renders) already carry the
  // regular header. The effect below keeps it set and resets to 'lead' when the
  // user navigates away.
  if (typeof window !== 'undefined') setFuturesApiMode('regular');
  useEffect(() => {
    setFuturesApiMode('regular');
    return () => setFuturesApiMode('lead');
  }, []);

  return (
    <>
      <div className="border-b border-amber-500/30 bg-amber-500/10 px-4 py-2 text-center text-xs font-semibold text-amber-300">
        Regular Futures Terminal — trading your normal KuCoin Futures account (not Lead copy-trading).
        Uses your Regular Futures API keys from Setup.
      </div>
      <FuturesTerminal />
    </>
  );
}
