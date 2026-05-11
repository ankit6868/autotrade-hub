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

// ── Futures Trading Terminal Types ──────────────────────────────────────

export interface OrderBookLevel {
  price: string;
  size: string;
}

export interface OrderBookData {
  symbol: string;
  asks: [string, string][];
  bids: [string, string][];
  ts?: number;
}

export interface FuturesRecentTrade {
  sequence: number;
  price: string;
  size: number;
  side: string;
  ts: number;
}

export interface FuturesPosition {
  id: string | number;
  pair: string;
  side: string;
  entry_price: number;
  current_price: number;
  amount: number;
  leverage: number;
  liquidation_price: number | null;
  stoploss_price: number | null;
  tp_price?: number | null;
  entry_time: string;
  mode: string;
  market_type: string;
  unrealized_pnl: number;
}

export interface FuturesOrder {
  order_id: string;
  db_id?: number;
  symbol: string;
  side: string;
  order_type: string;
  size: number;
  price: number | null;
  stop_price: number | null;
  leverage: number;
  margin_mode: string;
  status: string;
  filled_size?: number;
  filled_price?: number;
  tp_price?: number | null;
  sl_price?: number | null;
  created_at: string;
  filled_at?: string | null;
}

export interface FuturesAccount {
  mode: string;
  balance: number;
  equity: number;
  unrealized_pnl: number;
  used_margin: number;
  available_balance: number;
  position_count: number;
  currency: string;
}

export interface FuturesContract {
  symbol: string;
  baseCurrency: string;
  multiplier: number;
  tickSize: number;
  lotSize: number;
  maxLeverage: number;
  isInverse: boolean;
  status: string;
}

export interface FuturesBot {
  id: number;
  strategy_name: string;
  strategy_id: number | null;
  mode: string;
  pairs: string;
  leverage: number;
  timeframe: string;
  wallet: number;
  is_running: boolean;
  total_trades: number;
  total_pnl: number;
  stoploss: number;
  takeprofit: number;
  created_at: string;
}
