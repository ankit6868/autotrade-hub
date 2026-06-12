// Per-strategy UI option toggles. Each flag maps to a class attribute the
// engine/backtester overrides on the strategy instance (via strategy_flags).
// Only strategies listed here show controls; everything else is unaffected.
// Shared by the Futures Backtest page and the Bot panel so both stay in sync.

export type StratFlag = {
  key: string;
  label: string;
  hint: string;
  type?: 'bool' | 'number';        // default 'bool'
  default: boolean | number;
  min?: number;                    // number type only
  max?: number;
};

export const STRATEGY_FLAGS: Record<string, StratFlag[]> = {
  StrategyAsh: [
    {
      key: 'use_exit_signals',
      label: 'CHoCH early-exit',
      default: false,
      hint: 'Close a trade early when an opposite Change-of-Character (bias flip) prints. OFF = let the trade run to its SL / TP / ARM targets (recommended). ON can whipsaw and preempt take-profits.',
    },
  ],
  LorentzianClassifier: [
    {
      key: 'max_hold_candles',
      type: 'number',
      label: 'Bar hold',
      default: 4,
      min: 0,
      max: 50,
      hint: 'Exit after this many bars — jdehorty’s default is 4 (the LDC is a fixed-hold strategy, hence "max_hold_expired"). Set 0 to DISABLE the fixed exit and let trades run on signal flips / kernel / your SL-TP instead.',
    },
    {
      key: 'USE_DYNAMIC_EXITS',
      label: 'Dynamic kernel exit',
      default: false,
      hint: 'Exit when the Nadaraya-Watson kernel flips against the position (jdehorty’s "Use Dynamic Exits"). OFF = exit on the bar-hold + signal flips.',
    },
    {
      key: 'USE_ATR_STOPS',
      label: 'ATR structural stops',
      default: false,
      hint: 'Use the strategy’s own wide 3xATR / 6xATR stop instead of your configured SL/TP. OFF = your configured SL/TP drives both backtest and live (recommended for parity).',
    },
    {
      key: 'USE_EMA_FILTER',
      label: 'EMA(200) trend filter',
      default: false,
      hint: 'Only allow longs above / shorts below the 200 EMA (jdehorty’s Feb-6 filter). Fewer, trend-aligned trades.',
    },
    {
      key: 'USE_SMA_FILTER',
      label: 'SMA(200) trend filter',
      default: false,
      hint: 'Only allow longs above / shorts below the 200 SMA. Fewer, trend-aligned trades.',
    },
  ],
};

// Build the strategy_flags payload for a strategy, given the user's control
// state (falls back to each flag's default). Returns undefined when the
// strategy has no flags.
export function buildStrategyFlags(
  strategyName: string | undefined,
  state: Record<string, boolean | number>,
): Record<string, boolean | number> | undefined {
  const flags = (strategyName && STRATEGY_FLAGS[strategyName]) || [];
  if (!flags.length) return undefined;
  const out: Record<string, boolean | number> = {};
  for (const f of flags) {
    const v = state[f.key];
    out[f.key] = v !== undefined && v !== null ? v : f.default;
  }
  return out;
}
