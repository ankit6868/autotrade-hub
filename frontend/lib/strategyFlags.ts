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
  // number type only: when set, the control renders as an On/Off switch + a
  // stepper. Toggling Off sends this value (typically 0 = "disabled"); toggling
  // On restores the stepper value (default). Lets the user RAISE or DISABLE a
  // bar-hold timer with one click instead of remembering "0 means off".
  disableValue?: number;
  // number type only: value applied when the switch is toggled ON for an
  // OFF-by-default flag (i.e. when `default` itself equals `disableValue`).
  // Without this, turning ON would re-apply the default (= the disable value)
  // and the switch could never leave OFF. Defaults to 20 when omitted.
  onValue?: number;
};

export const STRATEGY_FLAGS: Record<string, StratFlag[]> = {
  GaulsLiquiditySweep: [
    {
      key: 'SWING_LOOKBACK', type: 'number', label: 'Pool lookback', default: 20, min: 5, max: 100,
      hint: 'Bars back used to define the liquidity pools (recent swing high = BSL, swing low = SSL). Larger = only sweeps of bigger/older levels qualify (fewer, higher-quality signals).',
    },
    {
      key: 'VOL_PERCENTILE', type: 'number', label: 'Volume percentile', default: 0.7, min: 0.5, max: 0.95, step: 0.05,
      hint: 'The sweep candle’s volume must exceed this percentile of recent volume (the book’s ~70% rule). Higher = demand a bigger volume spike = stricter, fewer trades.',
    },
    {
      key: 'WICK_RATIO', type: 'number', label: 'Rejection wick ratio', default: 0.5, min: 0.3, max: 0.9, step: 0.05,
      hint: 'How dominant the rejection wick must be (wick ≥ this × candle range). Higher = only strong, clean rejections qualify.',
    },
    {
      key: 'SL_ATR_BUFFER', type: 'number', label: 'Stop buffer (×ATR)', default: 0.25, min: 0, max: 2, step: 0.05,
      hint: 'Stop is placed this × ATR BEYOND the swept extreme (not on the obvious level, where stops get hunted). Larger = more room, wider stop.',
    },
    {
      key: 'MIN_RR', type: 'number', label: 'Min reward:risk', default: 1.5, min: 1, max: 5, step: 0.5,
      hint: 'If the opposite pool target is closer than this multiple of risk, the target is pushed out to this R. Higher = bigger winners required (lower hit rate, larger wins).',
    },
  ],
  StrategyAsh: [
    {
      key: 'max_hold_candles',
      type: 'number',
      label: 'Bar-hold timer',
      default: 60,
      min: 1,
      max: 500,
      disableValue: 0,
      hint: 'Force-close after this many bars (StrategyAsh’s 5h backstop = 60 on 5m). If "max_hold_expired" is dominating your exits, switch this OFF so trades exit only on SL / TP / CHoCH / signal flips. Tip: also switch SL/TP source to "From strategy (structural)" so the strategy’s tighter SMC stops drive exits.',
    },
    {
      key: 'use_exit_signals',
      label: 'CHoCH early-exit',
      default: false,
      hint: 'Close a trade early when an opposite Change-of-Character (bias flip) prints. OFF = let the trade run to its SL / TP / ARM targets. ON exits on structure flips (your CHoCH-ON run shows this working).',
    },
  ],
  LorentzianClassifier: [
    // ── Exits ──
    {
      key: 'max_hold_candles', type: 'number', label: 'Bar-hold timer', default: 4, min: 1, max: 50, disableValue: 0,
      hint: 'Exit after this many bars — jdehorty’s default is 4 (the LDC is a fixed-hold strategy, hence "max_hold_expired"). Switch OFF to disable the fixed exit and let trades run on signal flips / kernel / your SL-TP instead.',
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
