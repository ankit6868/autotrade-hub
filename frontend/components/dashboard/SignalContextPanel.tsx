'use client';

/**
 * SignalContextPanel — shown on Backtest / Paper-Trade / Live-Trade pages
 * when the user arrives from the Opportunities scanner.
 *
 * Displays the live indicator snapshot (RSI, MACD, ADX, BB …),
 * the strategy's entry/exit conditions derived from those values,
 * and the reasoning bullets that drove the opportunity score.
 */

interface SignalContextProps {
  pair: string;
  strategy?: string | null;
  timeframe?: string | null;
  score?: string | null;
  action?: string | null;      // 'buy' | 'sell' | 'strong_buy' etc.
  rsi?: string | null;
  adx?: string | null;
  macd?: string | null;
  bbPos?: string | null;       // 0–1 bollinger band position
  volChange?: string | null;   // volume_change_pct
  entryQuality?: string | null;
  confidence?: string | null;
  reasoning?: string | null;   // pipe-separated reasoning bullets
}

function IndicatorPill({
  label,
  value,
  interpretation,
  color,
}: {
  label: string;
  value: string;
  interpretation: string;
  color: 'green' | 'red' | 'yellow' | 'neutral';
}) {
  const colorClass = {
    green: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300',
    red: 'bg-red-500/10 border-red-500/30 text-red-300',
    yellow: 'bg-yellow-500/10 border-yellow-500/30 text-yellow-300',
    neutral: 'bg-slate-500/10 border-slate-500/30 text-slate-300',
  }[color];

  return (
    <div className={`px-3 py-2 rounded-lg border ${colorClass}`}>
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs text-slate-400 uppercase tracking-wider font-medium">{label}</span>
        <span className="text-sm font-bold font-mono">{value}</span>
      </div>
      <p className="text-xs mt-0.5 opacity-75">{interpretation}</p>
    </div>
  );
}

function conditionColor(
  indicator: string,
  value: number | null,
  action: string
): 'green' | 'red' | 'yellow' | 'neutral' {
  if (value === null) return 'neutral';
  const isBuy = action.toLowerCase().includes('buy');
  switch (indicator) {
    case 'rsi':
      if (isBuy) return value < 35 ? 'green' : value < 50 ? 'yellow' : 'red';
      return value > 65 ? 'green' : value > 50 ? 'yellow' : 'red';
    case 'adx':
      return value >= 25 ? 'green' : value >= 20 ? 'yellow' : 'red';
    case 'macd':
      if (isBuy) return value > 0 ? 'green' : value > -0.001 ? 'yellow' : 'red';
      return value < 0 ? 'green' : value < 0.001 ? 'yellow' : 'red';
    case 'bb':
      if (isBuy) return value < 0.2 ? 'green' : value < 0.4 ? 'yellow' : 'red';
      return value > 0.8 ? 'green' : value > 0.6 ? 'yellow' : 'red';
    case 'vol':
      return value > 0.2 ? 'green' : value > 0 ? 'yellow' : 'neutral';
    default:
      return 'neutral';
  }
}

function rsiInterpretation(rsi: number, action: string): string {
  const isBuy = action.toLowerCase().includes('buy');
  if (rsi < 30) return isBuy ? '✅ Oversold — ideal buy zone' : '⚠️ Oversold, risky to sell';
  if (rsi < 50) return isBuy ? '🟡 Neutral — moderate entry' : '🔴 Below midline, weak sell';
  if (rsi < 70) return isBuy ? '🔴 Overbought, late entry' : '🟡 Approaching sell zone';
  return isBuy ? '❌ Overbought — avoid buying' : '✅ Overbought — sell signal';
}

function adxInterpretation(adx: number): string {
  if (adx >= 40) return '✅ Very strong trend';
  if (adx >= 25) return '✅ Trending market';
  if (adx >= 20) return '🟡 Weak trend forming';
  return '❌ Sideways / choppy market';
}

function macdInterpretation(macd: number, action: string): string {
  const isBuy = action.toLowerCase().includes('buy');
  if (macd > 0.01) return isBuy ? '✅ Bullish momentum' : '❌ Bullish, avoid selling';
  if (macd > 0) return isBuy ? '🟡 Slightly bullish' : '🟡 Near zero crossover';
  if (macd > -0.01) return isBuy ? '🟡 Near crossover' : '✅ Slightly bearish';
  return isBuy ? '❌ Bearish momentum' : '✅ Strong bearish momentum';
}

function bbInterpretation(pos: number, action: string): string {
  const isBuy = action.toLowerCase().includes('buy');
  if (pos < 0.1) return isBuy ? '✅ Near lower band (buy zone)' : '❌ Lower band, oversold';
  if (pos < 0.3) return isBuy ? '🟡 Below midline' : '🔴 Not in sell zone';
  if (pos < 0.7) return isBuy ? '🔴 Mid-band, late entry' : '🟡 Approaching upper band';
  if (pos < 0.9) return isBuy ? '❌ Near upper band' : '🟡 Near upper band (sell zone)';
  return isBuy ? '❌ At upper band, avoid' : '✅ At upper band (sell zone)';
}

export default function SignalContextPanel({
  pair,
  strategy,
  timeframe,
  score,
  action = 'buy',
  rsi,
  adx,
  macd,
  bbPos,
  volChange,
  entryQuality,
  confidence,
  reasoning,
}: SignalContextProps) {
  const act = action || 'buy';
  const isBuy = act.toLowerCase().includes('buy');

  const rsiVal = rsi != null ? parseFloat(rsi) : null;
  const adxVal = adx != null ? parseFloat(adx) : null;
  const macdVal = macd != null ? parseFloat(macd) : null;
  const bbVal = bbPos != null ? parseFloat(bbPos) : null;
  const volVal = volChange != null ? parseFloat(volChange) : null;
  const confVal = confidence != null ? (parseFloat(confidence) * 100) : null;
  const eqVal = entryQuality != null ? parseFloat(entryQuality) : null;

  const reasons = reasoning ? reasoning.split('|').filter(Boolean) : [];

  const overallScore = score ? parseInt(score) : null;
  const scoreColor = overallScore != null
    ? overallScore >= 70 ? 'text-emerald-400'
    : overallScore >= 55 ? 'text-amber-400'
    : 'text-red-400'
    : 'text-slate-400';

  // Entry conditions summary
  const entryConditions: { label: string; met: boolean; note: string }[] = [];

  if (rsiVal !== null) {
    const met = isBuy ? rsiVal < 50 : rsiVal > 50;
    entryConditions.push({
      label: isBuy ? 'RSI not overbought (< 50)' : 'RSI elevated (> 50)',
      met,
      note: `Current: ${rsiVal.toFixed(1)}`,
    });
  }
  if (adxVal !== null) {
    entryConditions.push({
      label: 'ADX trending (> 20)',
      met: adxVal >= 20,
      note: `Current: ${adxVal.toFixed(1)} ${adxVal >= 25 ? '(strong)' : adxVal >= 20 ? '(moderate)' : '(weak)'}`,
    });
  }
  if (macdVal !== null) {
    const met = isBuy ? macdVal > -0.005 : macdVal < 0.005;
    entryConditions.push({
      label: isBuy ? 'MACD bullish / crossing up' : 'MACD bearish / crossing down',
      met,
      note: `Current: ${macdVal.toFixed(4)}`,
    });
  }
  if (bbVal !== null) {
    const met = isBuy ? bbVal < 0.5 : bbVal > 0.5;
    entryConditions.push({
      label: isBuy ? 'Price below BB midline' : 'Price above BB midline',
      met,
      note: `BB position: ${(bbVal * 100).toFixed(0)}%`,
    });
  }

  const metCount = entryConditions.filter((c) => c.met).length;
  const totalCond = entryConditions.length;

  return (
    <div className={`mb-6 rounded-xl border overflow-hidden ${
      isBuy ? 'border-emerald-500/30' : 'border-red-500/30'
    }`}>
      {/* Header */}
      <div className={`px-4 py-3 flex items-center justify-between gap-4 ${
        isBuy ? 'bg-emerald-500/10' : 'bg-red-500/10'
      }`}>
        <div className="flex items-center gap-3 flex-wrap">
          <span className="text-xl">{isBuy ? '📈' : '📉'}</span>
          <div>
            <span className={`font-bold text-sm uppercase tracking-wide ${isBuy ? 'text-emerald-300' : 'text-red-300'}`}>
              {act.replace('_', ' ')} Signal
            </span>
            <span className="text-slate-400 text-xs ml-2">
              {pair} · {strategy || '—'} · {timeframe || '—'}
            </span>
          </div>
        </div>
        <div className="flex items-center gap-3 shrink-0 text-xs">
          {overallScore !== null && (
            <div className="text-center">
              <div className={`text-xl font-bold ${scoreColor}`}>{overallScore}</div>
              <div className="text-slate-500">score</div>
            </div>
          )}
          {eqVal !== null && (
            <div className="text-center">
              <div className="text-lg font-bold text-white">{eqVal.toFixed(0)}</div>
              <div className="text-slate-500">entry</div>
            </div>
          )}
          {confVal !== null && (
            <div className="text-center">
              <div className="text-lg font-bold text-white">{confVal.toFixed(0)}%</div>
              <div className="text-slate-500">conf.</div>
            </div>
          )}
        </div>
      </div>

      <div className="p-4 grid md:grid-cols-2 gap-4">
        {/* Left: Live Indicators */}
        <div>
          <h3 className="text-xs font-semibold uppercase text-slate-400 mb-3 tracking-wider">
            📊 Live Indicators at Signal Time
          </h3>
          <div className="space-y-2">
            {rsiVal !== null && (
              <IndicatorPill
                label="RSI"
                value={rsiVal.toFixed(1)}
                interpretation={rsiInterpretation(rsiVal, act)}
                color={conditionColor('rsi', rsiVal, act)}
              />
            )}
            {adxVal !== null && (
              <IndicatorPill
                label="ADX"
                value={adxVal.toFixed(1)}
                interpretation={adxInterpretation(adxVal)}
                color={conditionColor('adx', adxVal, act)}
              />
            )}
            {macdVal !== null && (
              <IndicatorPill
                label="MACD"
                value={macdVal.toFixed(4)}
                interpretation={macdInterpretation(macdVal, act)}
                color={conditionColor('macd', macdVal, act)}
              />
            )}
            {bbVal !== null && (
              <IndicatorPill
                label="BB Position"
                value={`${(bbVal * 100).toFixed(0)}%`}
                interpretation={bbInterpretation(bbVal, act)}
                color={conditionColor('bb', bbVal, act)}
              />
            )}
            {volVal !== null && (
              <IndicatorPill
                label="Volume Δ"
                value={`${volVal >= 0 ? '+' : ''}${(volVal * 100).toFixed(1)}%`}
                interpretation={volVal > 0.3 ? '✅ High volume — strong signal' : volVal > 0 ? '🟡 Moderate volume' : '⚠️ Low volume'}
                color={conditionColor('vol', volVal, act)}
              />
            )}
          </div>
        </div>

        {/* Right: Entry Conditions + Reasoning */}
        <div className="space-y-4">
          {/* Entry conditions checklist */}
          {entryConditions.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold uppercase text-slate-400 mb-2 tracking-wider flex items-center gap-2">
                ✅ Entry Conditions
                <span className={`text-xs px-1.5 py-0.5 rounded font-mono ${
                  metCount === totalCond ? 'bg-emerald-500/20 text-emerald-400' :
                  metCount >= totalCond / 2 ? 'bg-yellow-500/20 text-yellow-400' :
                  'bg-red-500/20 text-red-400'
                }`}>{metCount}/{totalCond}</span>
              </h3>
              <ul className="space-y-1.5">
                {entryConditions.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs">
                    <span className={`shrink-0 mt-0.5 ${c.met ? 'text-emerald-400' : 'text-red-400'}`}>
                      {c.met ? '✓' : '✗'}
                    </span>
                    <div>
                      <span className={c.met ? 'text-slate-200' : 'text-slate-400'}>{c.label}</span>
                      <span className="text-slate-500 ml-1">({c.note})</span>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Exit conditions */}
          <div>
            <h3 className="text-xs font-semibold uppercase text-slate-400 mb-2 tracking-wider">
              🚪 Exit Conditions
            </h3>
            <ul className="space-y-1 text-xs text-slate-400">
              <li className="flex items-start gap-2">
                <span className="text-red-400 shrink-0">🛑</span>
                <span>Stop-Loss triggers to limit downside (configurable below)</span>
              </li>
              <li className="flex items-start gap-2">
                <span className="text-emerald-400 shrink-0">🎯</span>
                <span>Take-Profit at target % (configurable below)</span>
              </li>
              {rsiVal !== null && (
                <li className="flex items-start gap-2">
                  <span className="text-yellow-400 shrink-0">📊</span>
                  <span>RSI {isBuy ? '> 70 (overbought)' : '< 30 (oversold)'} — strategy signal reversal</span>
                </li>
              )}
              <li className="flex items-start gap-2">
                <span className="text-blue-400 shrink-0">↩</span>
                <span>MACD {isBuy ? 'bearish crossover' : 'bullish crossover'} — momentum flip</span>
              </li>
            </ul>
          </div>

          {/* Reasoning */}
          {reasons.length > 0 && (
            <div>
              <h3 className="text-xs font-semibold uppercase text-slate-400 mb-2 tracking-wider">
                💡 Why this trade
              </h3>
              <ul className="space-y-1">
                {reasons.map((r, i) => (
                  <li key={i} className="flex items-start gap-2 text-xs text-slate-300">
                    <span className="text-brand-400 shrink-0">•</span>
                    <span>{r}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* Footer disclaimer */}
      <div className="px-4 py-2 bg-[#0f1a2e] border-t border-[#2a3a52]/50 text-xs text-slate-500 flex items-center gap-2">
        <span>⚡</span>
        <span>
          These are <strong className="text-slate-400">live indicator readings</strong> from the scanner.
          {' '}Set your stop-loss and take-profit below before starting the bot.
        </span>
      </div>
    </div>
  );
}
