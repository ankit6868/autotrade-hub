'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import { SidebarSignOut } from '@/components/AuthShell';

const nav = [
  { href: '/',                  label: 'Dashboard',          icon: '⚡' },
  { href: '/setup',             label: 'Setup',              icon: '⚙️' },
  { href: '/strategy/upload',   label: 'Upload Strategy',    icon: '📄' },
  { href: '/strategy/editor',   label: 'Strategy Editor',    icon: '✏️' },
  { href: '/strategy/templates', label: 'Templates',         icon: '📋' },
  { href: '/opportunities',     label: 'Opportunities',      icon: '🎯' },
  { href: '/backtest',          label: 'Backtest',           icon: '📊' },
  // ── Spot Trading ──────────────────────────────────────────────────
  { href: '/paper-trade',       label: 'Paper Trade',        icon: '📝' },
  { href: '/live',              label: 'Live Trading',       icon: '🔴' },
  // ── Futures Trading ───────────────────────────────────────────────
  { href: '/futures-paper',     label: 'Futures Paper',      icon: '📊' },
  { href: '/futures-live',      label: 'Futures Live',       icon: '⚡' },
  { href: '/futures-backtest',  label: 'Futures Backtest',   icon: '🔬' },
  // ── Advanced ──────────────────────────────────────────────────────
  { href: '/copy-trading',      label: 'Copy Trading',       icon: '📡' },
  { href: '/auto-trade',        label: 'Auto-Trade',         icon: '🤖' },
  { href: '/history',           label: 'History',            icon: '📈' },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  useEffect(() => {
    if (typeof document === 'undefined') return;
    document.body.style.overflow = open ? 'hidden' : '';
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
      {/* Mobile top bar */}
      <header
        suppressHydrationWarning
        className="md:hidden fixed top-0 inset-x-0 h-14 z-40 flex items-center justify-between px-4 border-b border-white/[0.06]"
        style={{
          paddingTop: 'env(safe-area-inset-top)',
          background: 'rgba(10, 14, 28, 0.7)',
          backdropFilter: 'blur(18px) saturate(160%)',
          WebkitBackdropFilter: 'blur(18px) saturate(160%)',
        }}
      >
        <button
          aria-label={open ? 'Close menu' : 'Open menu'}
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
          className="p-2 -ml-2 rounded-lg text-slate-200 hover:bg-white/5 active:scale-95 transition"
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
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
        <Link href="/" className="flex items-center gap-2">
          <span className="text-base font-semibold text-white tracking-tight">AutoTrade Hub</span>
        </Link>
        <div className="w-9" />
      </header>

      {/* Backdrop */}
      {open && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm animate-fade-in"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar */}
      <aside
        suppressHydrationWarning
        className={[
          'fixed left-0 top-0 h-screen w-72 md:w-64 z-50 flex flex-col',
          'border-r border-white/[0.06]',
          'transform transition-transform duration-300 ease-out will-change-transform',
          open ? 'translate-x-0' : '-translate-x-full',
          'md:translate-x-0',
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
        {/* Brand */}
        <div className="px-5 py-5 border-b border-white/[0.06] flex items-center justify-between">
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
          <button
            aria-label="Close menu"
            onClick={() => setOpen(false)}
            className="md:hidden p-1.5 rounded-lg text-slate-400 hover:text-white hover:bg-white/5 transition"
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
              <Link
                key={item.href}
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
