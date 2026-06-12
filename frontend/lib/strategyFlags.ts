// Per-strategy UI option controls. Each flag maps to a class attribute the
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
  step?: number;                   // number type only (default 1)
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
    // ── Exits ──
    {
      key: 'max_hold_candles', type: 'number', label: 'Bar hold', default: 4, min: 0, max: 50,
      hint: 'Exit after this many bars — jdehorty’s default is 4 (the LDC is a fixed-hold strategy, hence "max_hold_expired"). Set 0 to DISABLE the fixed exit and let trades run on signal flips / kernel / your SL-TP instead.',
    },
    {
      key: 'USE_DYNAMIC_EXITS', label: 'Dynamic kernel exit', default: false,
      hint: 'Exit when the Nadaraya-Watson kernel flips against the position (jdehorty’s "Use Dynamic Exits"). OFF = exit on the bar-hold + signal flips.',
    },
    {
      key: 'USE_ATR_STOPS', label: 'ATR structural stops', default: false,
      hint: 'Use the strategy’s own wide 3xATR / 6xATR stop instead of your configured SL/TP. OFF = your configured SL/TP drives both backtest and live (recommended for parity).',
    },
    // ── ML ──
    {
      key: 'NEIGHBORS_COUNT', type: 'number', label: 'Neighbors count', default: 8, min: 1, max: 100,
      hint: 'How many nearest neighbours the ANN sums for each prediction (jdehorty default 8). More = smoother, fewer flips; fewer = more reactive.',
    },
    // ── Filters ──
    {
      key: 'USE_VOLATILITY_FILTER', label: 'Volatility filter', default: true,
      hint: 'Only trade when recent ATR > historical ATR (avoid dead, low-vol ranges). jdehorty default ON.',
    },
    {
      key: 'USE_REGIME_FILTER', label: 'Regime filter', default: true,
      hint: 'Trend-detection filter using a KLMF slope. Only trade when the normalized slope ≥ Regime threshold. jdehorty default ON.',
    },
    {
      key: 'REGIME_THRESHOLD', type: 'number', label: 'Regime threshold', default: -0.1, min: -10, max: 10, step: 0.1,
      hint: 'Trending/ranging cutoff for the Regime filter (jdehorty default -0.1). Higher = stricter (fewer trades).',
    },
    {
      key: 'USE_ADX_FILTER', label: 'ADX filter', default: false,
      hint: 'Only trade when ADX > ADX threshold (trend strength). jdehorty default OFF.',
    },
    {
      key: 'ADX_THRESHOLD', type: 'number', label: 'ADX threshold', default: 20, min: 0, max: 100,
      hint: 'Trend-strength cutoff for the ADX filter (jdehorty default 20). Only used when the ADX filter is on.',
    },
    {
      key: 'USE_EMA_FILTER', label: 'EMA(200) trend filter', default: false,
      hint: 'Only allow longs above / shorts below the 200 EMA (jdehorty’s Feb-6 filter). Fewer, trend-aligned trades.',
    },
    {
      key: 'USE_SMA_FILTER', label: 'SMA(200) trend filter', default: false,
      hint: 'Only allow longs above / shorts below the 200 SMA. Fewer, trend-aligned trades.',
    },
    // ── Kernel ──
    {
      key: 'USE_KERNEL_FILTER', label: 'Trade with kernel', default: true,
      hint: 'Only enter when the Nadaraya-Watson kernel agrees with the trade direction (jdehorty default ON).',
    },
    {
      key: 'KERNEL_LOOKBACK', type: 'number', label: 'Kernel lookback', default: 8, min: 3, max: 50,
      hint: 'Bars used for the kernel regression estimate (jdehorty default 8, range 3-50).',
    },
    {
      key: 'KERNEL_REL_WEIGHT', type: 'number', label: 'Kernel rel. weight', default: 8, min: 0.25, max: 25, step: 0.25,
      hint: 'Relative weighting of the rational-quadratic kernel (jdehorty default 8, range 0.25-25). Lower = smoother.',
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
