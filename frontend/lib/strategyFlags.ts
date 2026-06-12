// Per-strategy UI option toggles. Each flag maps to a boolean class attribute
// the engine/backtester overrides on the strategy instance (via strategy_flags).
// Only strategies listed here show toggles; everything else is unaffected.
// Shared by the Futures Backtest page and the Bot panel so both stay in sync.

export type StratFlag = { key: string; label: string; hint: string; default: boolean };

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
      key: 'USE_DYNAMIC_EXITS',
      label: 'Dynamic kernel exit',
      default: false,
      hint: 'Exit when the Nadaraya-Watson kernel flips against the position (jdehorty’s "Use Dynamic Exits"). OFF = exit on the 4-bar hold + signal flips.',
    },
    {
      key: 'USE_ATR_STOPS',
      label: 'ATR structural stops',
      default: false,
      hint: 'Use the strategy’s own wide 3xATR / 6xATR stop instead of your configured SL/TP. OFF = your configured SL/TP drives both backtest and live (recommended for parity).',
    },
  ],
};

// Build the strategy_flags payload for a strategy, given the user's toggle
// state (falls back to each flag's default). Returns undefined when the
// strategy has no flags.
export function buildStrategyFlags(
  strategyName: string | undefined,
  state: Record<string, boolean>,
): Record<string, boolean> | undefined {
  const flags = (strategyName && STRATEGY_FLAGS[strategyName]) || [];
  if (!flags.length) return undefined;
  const out: Record<string, boolean> = {};
  for (const f of flags) out[f.key] = state[f.key] ?? f.default;
  return out;
}
