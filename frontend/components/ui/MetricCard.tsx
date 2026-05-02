interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: 'default' | 'profit' | 'loss';
}

export default function MetricCard({ title, value, subtitle, color = 'default' }: Props) {
  const valueColor =
    color === 'profit' ? 'text-emerald-400' : color === 'loss' ? 'text-red-400' : 'text-white';

  return (
    <div className="card">
      <p className="text-sm text-slate-400 mb-1">{title}</p>
      <p className={`text-2xl font-bold ${valueColor}`}>{value}</p>
      {subtitle && <p className="text-xs text-slate-500 mt-1">{subtitle}</p>}
    </div>
  );
}
