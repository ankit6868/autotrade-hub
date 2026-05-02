'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

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

  return (
    <aside suppressHydrationWarning className="fixed left-0 top-0 h-screen w-64 bg-[#111827] border-r border-[#2a3a52] flex flex-col z-50">
      <div className="p-6 border-b border-[#2a3a52]">
        <h1 className="text-xl font-bold text-white">AutoTrade Hub</h1>
        <p className="text-xs text-slate-400 mt-1">Free AI Trading Platform</p>
      </div>

      <nav className="flex-1 p-4 space-y-1 overflow-y-auto">
        {nav.map((item) => {
          const active = pathname === item.href || (item.href !== '/' && pathname.startsWith(item.href));
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm transition-colors ${
                active
                  ? 'bg-brand-600/20 text-brand-400 font-medium'
                  : 'text-slate-400 hover:text-white hover:bg-[#1a2236]'
              }`}
            >
              <span className="text-lg">{item.icon}</span>
              {item.label}
            </Link>
          );
        })}
      </nav>

      <div className="p-4 border-t border-[#2a3a52]">
        <button
          onClick={async () => {
            if (confirm('EMERGENCY STOP: This will halt all trading immediately. Continue?')) {
              await fetch('/api/trade/emergency-stop', { method: 'POST' });
              window.location.reload();
            }
          }}
          className="w-full btn-danger text-sm flex items-center justify-center gap-2"
        >
          🛑 Emergency Stop
        </button>
      </div>
    </aside>
  );
}
