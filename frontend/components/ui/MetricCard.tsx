interface Props {
  title: string;
  value: string | number;
  subtitle?: string;
  color?: 'default' | 'profit' | 'loss';
  /** "default" = glass card, "hero" = bold blue gradient (one per row), "accent" = subtle brand tint */
  variant?: 'default' | 'hero' | 'accent';
  icon?: React.ReactNode;
}

export default function MetricCard({
  title,
  value,
  subtitle,
  color = 'default',
  variant = 'default',
  icon,
}: Props) {
  if (variant === 'hero') {
    return (
      <div className="card-hero card-hover">
        <div className="relative z-10 flex items-start justify-between gap-3 mb-3">
          <p className="text-xs sm:text-sm text-white/80 uppercase tracking-wider font-medium">{title}</p>
          {icon && (
            <span className="icon-tile h-9 w-9 text-white/90">{icon}</span>
          )}
        </div>
        <p className="relative z-10 stat-xl text-white drop-shadow-[0_2px_8px_rgba(0,0,0,0.3)]">{value}</p>
        {subtitle && (
          <p className="relative z-10 text-xs sm:text-sm text-white/70 mt-2">{subtitle}</p>
        )}
      </div>
    );
  }

  // default / accent — frosted glass
  const valueColor =
    color === 'profit' ? 'text-emerald-400' : color === 'loss' ? 'text-red-400' : 'text-white';

  const accent =
    variant === 'accent'
      ? 'before:absolute before:inset-0 before:rounded-2xl before:bg-gradient-to-br before:from-brand-500/20 before:to-transparent before:opacity-100'
      : color === 'profit'
      ? 'before:absolute before:inset-0 before:rounded-2xl before:bg-gradient-to-br before:from-emerald-500/15 before:to-transparent before:opacity-90'
      : color === 'loss'
      ? 'before:absolute before:inset-0 before:rounded-2xl before:bg-gradient-to-br before:from-red-500/15 before:to-transparent before:opacity-90'
      : 'before:absolute before:inset-0 before:rounded-2xl before:bg-gradient-to-br before:from-white/[0.04] before:to-transparent before:opacity-100';

  // Auto-shrink the value font when the rendered string is long so
  // numbers like "$1024.65" don't truncate to "$102..." Previously
  // `truncate` was clipping anything wider than the tile.
  const valueStr = String(value);
  const valueSizeClass =
    valueStr.length >= 12 ? 'text-base sm:text-lg 2xl:text-xl' :
    valueStr.length >= 9  ? 'text-lg sm:text-xl 2xl:text-2xl' :
    valueStr.length >= 7  ? 'text-xl sm:text-2xl 2xl:text-3xl' :
                            'text-2xl sm:text-3xl 2xl:text-4xl';

  return (
    <div className={`card card-hover relative overflow-hidden group ${accent}`}>
      <div className="relative z-10">
        <div className="flex items-start justify-between gap-2 mb-1.5">
          <p className="text-[11px] xs:text-xs sm:text-sm text-slate-400 uppercase tracking-wider font-medium truncate">
            {title}
          </p>
          {icon && (
            <span className="icon-tile h-7 w-7 text-slate-300 flex-shrink-0">{icon}</span>
          )}
        </div>
        <p className={`${valueSizeClass} ${valueColor} font-bold tracking-tight tabular-nums whitespace-nowrap`}
           title={valueStr}>
          {value}
        </p>
        {subtitle && (
          <p className="text-[11px] sm:text-xs text-slate-500 mt-1 truncate">{subtitle}</p>
        )}
      </div>
    </div>
  );
}
