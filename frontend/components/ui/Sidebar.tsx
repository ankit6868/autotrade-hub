'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { SidebarSignOut } from '@/components/AuthShell';

const nav = [
  { href: '/',                   label: 'Dashboard',          icon: '⚡',  section: null },
  { href: '/setup',              label: 'Setup',              icon: '⚙️',  section: null },
  // ── Strategy ──────────────────────────────────────────────────────
  { href: '/strategy/upload',    label: 'Upload Strategy',    icon: '📄',  section: 'STRATEGY' },
  { href: '/strategy/editor',    label: 'Strategy Editor',    icon: '✏️',  section: null },
  { href: '/strategy/templates', label: 'Templates',          icon: '📋',  section: null },
  // ── Futures Trading ───────────────────────────────────────────────
  // Futures Paper / Futures Live were collapsed into Futures Terminal
  // (which has the Paper/Live toggle in its top-right corner).
  { href: '/futures-trade',      label: 'Futures Terminal',   icon: '💹',  section: 'FUTURES' },
  { href: '/futures-backtest',   label: 'Futures Backtest',   icon: '🔬',  section: null },
  // ── Advanced ──────────────────────────────────────────────────────
  { href: '/auto-trade',         label: 'Auto-Trade',         icon: '🤖',  section: 'ADVANCED' },
  { href: '/history',            label: 'History',            icon: '📈',  section: null },
];

// Persist desktop sidebar state across page navigations so the user's
// "I prefer it collapsed" choice sticks. Mobile is always controlled by the
// toggle and resets to closed on route change (because route navigation in
// the overlay UX should auto-close the menu — different intent).
const SIDEBAR_LS_KEY = 'autotrade-sidebar-open';

function readPersistedOpen(): boolean {
  if (typeof window === 'undefined') return true;
  // Default to OPEN on desktop, CLOSED on mobile.
  const stored = window.localStorage.getItem(SIDEBAR_LS_KEY);
  if (stored === 'true') return true;
  if (stored === 'false') return false;
  return window.matchMedia('(min-width: 768px)').matches;
}

export default function Sidebar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);   // SSR-safe initial value; hydrated below

  // Hydrate from localStorage on first client render
  useEffect(() => {
    setOpen(readPersistedOpen());
  }, []);

  // Persist + sync the body data attribute on every state change so the
  // main content's CSS (see globals.css) can reflow alongside the sidebar.
  useEffect(() => {
    if (typeof document === 'undefined') return;
    document.body.dataset.sidebarOpen = open ? 'true' : 'false';
    try { window.localStorage.setItem(SIDEBAR_LS_KEY, String(open)); } catch { /* private mode */ }
  }, [open]);

  // On mobile, close the menu after navigating. On desktop we leave the
  // user's choice intact — closing on every click would defeat the purpose
  // of a persistent collapse.
  useEffect(() => {
    if (typeof window === 'undefined') return;
    const isMobile = window.matchMedia('(max-width: 767px)').matches;
    if (isMobile) setOpen(false);
  }, [pathname]);

  // Lock body scroll only when the sidebar is overlaying content (mobile).
  // On desktop it lives in normal layout flow so no scroll-lock needed.
  useEffect(() => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return;
    const isDesktop = window.matchMedia('(min-width: 768px)').matches;
    document.body.style.overflow = open && !isDesktop ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <>
      {/* Floating hamburger button — always visible (mobile + desktop) so
          users can collapse/expand the sidebar at will. Sits in the top-left
          corner; when the sidebar is OPEN on desktop, this button still
          works to close it (and is visible above the sidebar via z-60). */}
      <button
        type="button"
        aria-label={open ? 'Close menu' : 'Open menu'}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={[
          'fixed top-3 left-3 z-[60] inline-flex items-center justify-center',
          'h-9 w-9 rounded-lg text-slate-200',
          'bg-[#0f1830]/80 backdrop-blur border border-white/[0.08]',
          'hover:bg-[#162045] hover:text-white active:scale-95 transition',
          'shadow-lg shadow-black/30',
        ].join(' ')}
        style={{ marginTop: 'env(safe-area-inset-top)' }}
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          {open ? (
            <>
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </>
          ) : (
            <>
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </>
          )}
        </svg>
      </button>

      {/* Mobile top bar (brand + spacer next to the hamburger). Stays mobile
          only — desktop uses just the floating hamburger. */}
      <header
        suppressHydrationWarning
        className="md:hidden fixed top-0 inset-x-0 h-14 z-40 flex items-center justify-center px-4 border-b border-white/[0.06]"
        style={{
          paddingTop: 'env(safe-area-inset-top)',
          background: 'rgba(10, 14, 28, 0.7)',
          backdropFilter: 'blur(18px) saturate(160%)',
          WebkitBackdropFilter: 'blur(18px) saturate(160%)',
        }}
      >
        <Link href="/" className="flex items-center gap-2">
          <span className="text-base font-semibold text-white tracking-tight">AutoTrade Hub</span>
        </Link>
      </header>

      {/* Backdrop — mobile only (desktop just reflows main content) */}
      {open && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm animate-fade-in"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar. The `md:translate-x-0` override is gone — now the
          translate is purely driven by `open`, so toggling collapses the
          sidebar on desktop too. */}
      <aside
        suppressHydrationWarning
        className={[
          'fixed left-0 top-0 h-screen w-72 md:w-64 z-50 flex flex-col',
          'border-r border-white/[0.06]',
          'transform transition-transform duration-300 ease-out will-change-transform',
          open ? 'translate-x-0' : '-translate-x-full',
        ].join(' ')}
        style={{
          paddingTop: 'env(safe-area-inset-top)',
          paddingBottom: 'env(safe-area-inset-bottom)',
          background: 'rgba(10, 14, 28, 0.72)',
          backdropFilter: 'blur(20px) saturate(160%)',
          WebkitBackdropFilter: 'blur(20px) saturate(160%)',
          boxShadow: 'inset -1px 0 0 rgba(255,255,255,0.04)',
        }}
      >
        {/* Brand. Left padding bumped (pl-16) so the brand stays clear of
            the floating hamburger button that overlaps the top-left corner
            when the sidebar is open. */}
        <div className="pl-16 pr-5 py-5 border-b border-white/[0.06] flex items-center justify-between">
          <Link href="/" className="flex items-center gap-2.5 group">
            <span
              className="inline-flex h-10 w-10 items-center justify-center rounded-2xl text-white text-lg shadow-glow group-hover:scale-105 transition-transform"
              style={{
                background: 'linear-gradient(135deg, #1b6ff5 0%, #1747e8 60%, #0a2cb8 100%)',
                boxShadow: '0 8px 24px -8px rgba(27,111,245,0.6), inset 0 1px 0 rgba(255,255,255,0.2)',
              }}
            >
              ⚡
            </span>
            <div>
              <h1 className="text-base font-semibold text-white leading-tight">AutoTrade Hub</h1>
              <p className="text-[11px] text-slate-400 leading-tight">Free AI Trading</p>
            </div>
          </Link>
          {/* Internal close button removed — the floating hamburger at the
              top-left handles open AND close on both viewports. */}
          <button
            aria-label="Close menu"
            onClick={() => setOpen(false)}
            className="hidden p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-white/5 transition"
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        {/* Navigation */}
        <nav className="flex-1 px-3 py-4 space-y-1 overflow-y-auto">
          {nav.map((item) => {
            const active = pathname === item.href || (item.href !== '/' && pathname?.startsWith(item.href));
            return (
              <div key={item.href}>
                {/* Section label */}
                {item.section && (
                  <p className="px-3 pt-3 pb-1 text-[10px] font-semibold tracking-widest text-slate-600 uppercase">
                    {item.section}
                  </p>
                )}
                <Link
                  href={item.href}
                  className={[
                    'group relative flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm transition-all',
                    active
                      ? 'text-white font-medium'
                      : 'text-slate-400 hover:text-white hover:bg-white/[0.04]',
                  ].join(' ')}
                  style={
                    active
                      ? {
                          background:
                            'linear-gradient(90deg, rgba(27,111,245,0.22) 0%, rgba(27,111,245,0.04) 100%)',
                          boxShadow: 'inset 0 0 0 1px rgba(27,111,245,0.25)',
                        }
                      : undefined
                  }
                >
                  {active && (
                    <span className="absolute left-0 top-1.5 bottom-1.5 w-[3px] rounded-r-full bg-brand-400 shadow-[0_0_12px_rgba(27,111,245,0.8)]" />
                  )}
                  <span
                    className={[
                      'inline-flex h-7 w-7 items-center justify-center rounded-lg text-base transition-colors',
                      active ? 'bg-white/[0.08]' : 'bg-white/[0.03] group-hover:bg-white/[0.06]',
                    ].join(' ')}
                  >
                    {item.icon}
                  </span>
                  <span className="truncate">{item.label}</span>
                </Link>
              </div>
            );
          })}
        </nav>

        {/* Footer / sign out + emergency stop */}
        <div className="p-3 border-t border-white/[0.06] space-y-2">
          <SidebarSignOut />
          <button
            onClick={async () => {
              if (confirm('EMERGENCY STOP: This will halt all trading immediately. Continue?')) {
                try {
                  await fetch('/api/trade/emergency-stop', { method: 'POST' });
                } finally {
                  window.location.reload();
                }
              }
            }}
            className="w-full btn-danger text-sm"
          >
            🛑 Emergency Stop
          </button>
        </div>
      </aside>
    </>
  );
}
