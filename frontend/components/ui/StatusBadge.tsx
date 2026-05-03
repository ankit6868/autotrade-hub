'use client';

interface Props {
  status: 'running' | 'stopped' | 'error' | 'connected' | 'disconnected';
  label?: string;
}

const colors = {
  running:      { dot: 'bg-emerald-500',  ring: 'shadow-[0_0_0_4px_rgba(16,185,129,0.15)]', chip: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' },
  connected:    { dot: 'bg-emerald-500',  ring: 'shadow-[0_0_0_4px_rgba(16,185,129,0.15)]', chip: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300' },
  stopped:      { dot: 'bg-slate-400',    ring: 'shadow-[0_0_0_4px_rgba(148,163,184,0.15)]', chip: 'bg-slate-500/10 border-slate-500/30 text-slate-300' },
  disconnected: { dot: 'bg-red-500',      ring: 'shadow-[0_0_0_4px_rgba(239,68,68,0.15)]',  chip: 'bg-red-500/10 border-red-500/30 text-red-300' },
  error:        { dot: 'bg-red-500',      ring: 'shadow-[0_0_0_4px_rgba(239,68,68,0.15)]',  chip: 'bg-red-500/10 border-red-500/30 text-red-300' },
} as const;

export default function StatusBadge({ status, label }: Props) {
  const c = colors[status];
  const animated = status === 'running' || status === 'connected';
  return (
    <span className={`chip ${c.chip}`}>
      <span className={`relative inline-flex h-2 w-2 rounded-full ${c.dot} ${c.ring}`}>
        {animated && (
          <span className={`absolute inset-0 rounded-full ${c.dot} animate-ping opacity-60`} />
        )}
      </span>
      <span className="capitalize">{label || status}</span>
    </span>
  );
}
