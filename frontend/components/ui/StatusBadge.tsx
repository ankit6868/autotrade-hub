'use client';

interface Props {
  status: 'running' | 'stopped' | 'error' | 'connected' | 'disconnected';
  label?: string;
}

const colors = {
  running: 'bg-emerald-500',
  stopped: 'bg-slate-500',
  error: 'bg-red-500',
  connected: 'bg-emerald-500',
  disconnected: 'bg-red-500',
};

export default function StatusBadge({ status, label }: Props) {
  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span className={`w-2.5 h-2.5 rounded-full ${colors[status]} animate-pulse`} />
      <span className="text-slate-300">{label || status}</span>
    </span>
  );
}
