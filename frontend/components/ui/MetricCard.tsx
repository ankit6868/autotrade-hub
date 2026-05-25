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

  // Auto-shrink the value font based on character count. Tile widths
  // are ~180-220px with p-6 padding (24px each side = 48px), leaving
  // ~130-170px for the value text. Conservative size table prevents
  // ANY value from getting clipped, while still keeping short values
  // (like "+2.35%") visually prominent.
  const valueStr = String(value);
  const valueSizeClass =
    valueStr.length >= 11 ? 'text-xs sm:text-sm 2xl:text-base' :
    valueStr.length >= 9  ? 'text-sm sm:text-base 2xl:text-lg'  :
    valueStr.length >= 7  ? 'text-base sm:text-lg 2xl:text-xl'  :
    valueStr.length >= 5  ? 'text-lg sm:text-xl 2xl:text-2xl'   :
                            'text-2xl sm:text-3xl 2xl:text-4xl';

  // Removed `overflow-hidden` from the outer card so text can never be
  // clipped — the ::before gradient still clips itself via rounded-2xl
  // even without parent overflow-hidden (Tailwind rounded-2xl + absolute
  // inset-0 stays inside the visible card bounds).
  return (
    <div className={`card card-hover relative group ${accent}`}>
      <div className="relative z-10 min-w-0">
        <div className="flex items-start justify-between gap-2 mb-1.5">
          <p className="text-[11px] xs:text-xs sm:text-sm text-slate-400 uppercase tracking-wider font-medium truncate">
            {title}
          </p>
          {icon && (
            <span className="icon-tile h-7 w-7 text-slate-300 flex-shrink-0">{icon}</span>
          )}
        </div>
        <p className={`${valueSizeClass} ${valueColor} font-bold tracking-tight tabular-nums whitespace-nowrap overflow-visible`}
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
