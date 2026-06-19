import { useEffect, useRef } from 'react';

/**
 * Drop-in replacement for setInterval that only ticks while the browser tab is
 * VISIBLE. When the tab is hidden (backgrounded, minimised, another tab in
 * front, screen off) it pauses — zero network calls — and the moment the tab
 * becomes visible again it fires the callback once immediately, then resumes
 * the interval.
 *
 * Why this exists:
 *   Forgotten / background app tabs polling the API every few seconds were the
 *   dominant source of Supabase egress and Vercel edge-request / function-
 *   invocation usage (one poll = 1 edge request + 1 function invocation + 1 DB
 *   read). Pausing while hidden removes that waste with ZERO impact on what the
 *   user sees while watching — and ZERO impact on the trading bots, which run
 *   server-side on Railway independently of any browser tab.
 *
 * The callback is kept in a ref, so changing its identity between renders does
 * NOT restart the interval — only a change to `delayMs` / `enabled` does.
 */
export function useVisibleInterval(
  callback: () => void,
  delayMs: number,
  enabled: boolean = true,
): void {
  const saved = useRef(callback);
  useEffect(() => { saved.current = callback; }, [callback]);

  useEffect(() => {
    if (!enabled || delayMs <= 0) return;
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = () => saved.current();
    const start = () => { if (timer === null) timer = setInterval(tick, delayMs); };
    const stop = () => { if (timer !== null) { clearInterval(timer); timer = null; } };

    const onVisibility = () => {
      if (document.visibilityState === 'visible') {
        tick();    // refresh immediately on return so stale data never lingers
        start();
      } else {
        stop();    // hidden → stop polling entirely
      }
    };

    // Begin only if currently visible (effects run client-side, so document
    // exists; guard anyway for safety).
    if (typeof document === 'undefined' || document.visibilityState === 'visible') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      stop();
    };
  }, [delayMs, enabled]);
}
