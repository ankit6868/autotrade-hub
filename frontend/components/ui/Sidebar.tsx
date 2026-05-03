'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';

const nav = [
  { href: '/', label: 'Dashboard', icon: '⚡' },
  { href: '/setup', label: 'Setup', icon: '⚙️' },
  { href: '/strategy/upload', label: 'Upload Strategy', icon: '📄' },
  { href: '/strategy/editor', label: 'Strategy Editor', icon: '✏️' },
  { href: '/strategy/templates', label: 'Templates', icon: '📋' },
  { href: '/opportunities', label: 'Opportunities', icon: '🎯' },
  { href: '/backtest', label: 'Backtest', icon: '📊' },
  { href: '/paper-trade', label: 'Paper Trade', icon: '📝' },
  { href: '/live', label: 'Live Trading', icon: '🔴' },
  { href: '/auto-trade', label: 'Auto-Trade', icon: '🤖' },
  { href: '/history', label: 'History', icon: '📈' },
];

export default function Sidebar() {
  const pathname = usePathname();
  const [open, setOpen] = useState(false);

  // Close drawer whenever the route changes
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Lock body scroll while the mobile drawer is open
  useEffect(() => {
    if (typeof document === 'undefined') return;
    document.body.style.overflow = open ? 'hidden' : '';
    return () => {
      document.body.style.overflow = '';
    };
  }, [open]);

  // Close on Escape
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <>
      {/* Mobile top bar — visible below md */}
      <header
        suppressHydrationWarning
        className="md:hidden fixed top-0 inset-x-0 h-14 z-40 flex items-center justify-between px-4 bg-[#0d1424]/85 backdrop-blur-md border-b border-[#243153]"
        style={{ paddingTop: 'env(safe-area-inset-top)' }}
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
        {/* Spacer balances the menu button so the title stays centred */}
        <div className="w-9" />
      </header>

      {/* Backdrop — only on mobile when drawer open */}
      {open && (
        <div
          className="md:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm animate-fade-in"
          onClick={() => setOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* Sidebar — desktop fixed, mobile slide-in drawer */}
      <aside
        suppressHydrationWarning
        className={[
          'fixed left-0 top-0 h-screen w-72 md:w-64 z-50',
          'bg-[#0d1424] border-r border-[#243153] flex flex-col',
          'transform transition-transform duration-300 ease-out will-change-transform',
          open ? 'translate-x-0' : '-translate-x-full',
          'md:translate-x-0',
        ].join(' ')}
        style={{
          paddingTop: 'env(safe-area-inset-top)',
          paddingBottom: 'env(safe-area-inset-bottom)',
        }}
      >
        <div className="px-5 py-5 border-b border-[#243153] flex items-center justify-between">
          <Link href="/" className="flex items-center gap-2.5 group">
            <span className="inline-flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-brand-700 text-white text-lg shadow-[0_4px_16px_-4px_rgba(51,145,255,0.6)] group-hover:scale-105 transition-transform">
              ⚡
            </span>
            <div>
              <h1 className="text-base font-semibold text-white leading-tight">AutoTrade Hub</h1>
              <p className="text-[11px] text-slate-400 leading-tight">Free AI Trading</p>
            </div>
          </Link>
          {/* Close button inside drawer (mobile only) */}
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

        <nav className="flex-1 px-3 py-4 space-y-0.5 overflow-y-auto">
          {nav.map((item) => {
            const active = pathname === item.href || (item.href !== '/' && pathname?.startsWith(item.href));
            return (
              <Link
                key={item.href}
                href={item.href}
                className={[
                  'group relative flex items-center gap-3 px-3 py-2.5 rounded-xl text-sm transition-all',
                  active
                    ? 'bg-gradient-to-r from-brand-600/25 to-brand-600/5 text-white font-medium'
                    : 'text-slate-400 hover:text-white hover:bg-white/[0.04]',
                ].join(' ')}
              >
                {active && (
                  <span className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-r-full bg-brand-400" />
                )}
                <span className="text-base w-5 text-center">{item.icon}</span>
                <span className="truncate">{item.label}</span>
              </Link>
            );
          })}
        </nav>

        <div className="p-3 border-t border-[#243153]">
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
