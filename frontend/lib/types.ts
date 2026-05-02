export interface ConfigStatus {
  configured: boolean;
  has_kucoin: boolean;
  has_openrouter: boolean;
  preferred_model: string;
  max_position_pct: number;
  max_open_trades: number;
  max_daily_drawdown_pct: number;
  default_stoploss_pct: number;
  has_telegram: boolean;
}

export interface Strategy {
  id: number;
  name: string;
  description: string;
  original_text?: string;
  generated_code: string;
  model_used?: string;
  indicators?: string[];
  timeframe: string;
  pairs?: string[];
  stoploss: number;
  is_template: boolean;
  created_at: string;
}

export interface StrategyListItem {
  id: number;
  name: string;
  description: string;
  timeframe: string;
  is_template: boolean;
  created_at: string;
}

export interface BacktestResult {
  id: number;
  metrics: {
    total_profit: number;
    win_rate: number;
    max_drawdown: number;
    sharpe_ratio: number;
    total_trades: number;
    avg_duration: string;
  };
  trades: TradeRecord[];
  results: Record<string, unknown>;
}

export interface TradeRecord {
  id: number;
  pair: string;
  side: string;
  entry_price: number;
  exit_price?: number;
  amount: number;
  profit_pct?: number;
  profit_abs?: number;
  stoploss_price?: number;
  entry_time: string;
  exit_time?: string;
  exit_reason?: string;
  mode: string;
  strategy_id?: number;
  status?: string;
}

export interface BotStatus {
  running: boolean;
  mode: string;
  strategy: string;
  pid: number | null;
}

export interface MarketSignal {
  symbol: string;
  interval: string;
  summary?: {
    recommendation: string;
    buy: number;
    sell: number;
    neutral: number;
  };
  indicators?: Record<string, number | null>;
  error?: string;
}

export interface Candle {
  timestamp: number;
  open: number;
  close: number;
  high: number;
  low: number;
  volume: number;
}

export interface FreeModel {
  id: string;
  name: string;
  context_length: number;
}
