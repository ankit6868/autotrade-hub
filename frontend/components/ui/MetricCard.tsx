interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: 'default' | 'profit' | 'loss';
  icon?: React.ReactNode;
}

export default function MetricCard({ title, value, subtitle, color = 'default', icon }: Props) {
  const valueColor =
    color === 'profit' ? 'text-emerald-400' : color === 'loss' ? 'text-red-400' : 'text-white';

  const accent =
    color === 'profit'
      ? 'from-emerald-500/15 to-transparent'
      : color === 'loss'
      ? 'from-red-500/15 to-transparent'
      : 'from-brand-500/15 to-transparent';

  return (
    <div className="card card-hover relative overflow-hidden group">
      <div className={`pointer-events-none absolute inset-0 bg-gradient-to-br ${accent} opacity-60 group-hover:opacity-90 transition-opacity`} />
      <div className="relative">
        <div className="flex items-start justify-between gap-2 mb-1.5">
          <p className="text-xs sm:text-sm text-slate-400 truncate">{title}</p>
          {icon && <div className="text-slate-500">{icon}</div>}
        </div>
        <p className={`text-xl sm:text-2xl font-bold tracking-tight ${valueColor} truncate`}>{value}</p>
        {subtitle && <p className="text-[11px] sm:text-xs text-slate-500 mt-1 truncate">{subtitle}</p>}
      </div>
    </div>
  );
}
