export type TimeframeInfo = {
  tf: string;
  bars: number;
  firstTs: number | null;
  lastTs: number | null;
};

export type Market = {
  symbol: string;
  name: string | null;
  sector: string | null;
  assetClass: string;
  source: string | null;
  bars: number;
  firstTs: number | null;
  timeframes: TimeframeInfo[];
};

export type AdjustMode = 'none' | 'split' | 'total';

/* -- engine ---------------------------------------------------------------- */

export type StrategyInfo = {
  name: string;
  title: string;
  description: string;
  style: string;
  timeframes: string[];
  defaults: Record<string, number>;
  labels: Record<string, string>;
  combos: number;          // grid combinations a sweep runs, after constraints
};

export type Costs = { commission_pct: number; slippage_bps: number };

export type Metrics = {
  net_pct: number | null;
  cagr_pct: number | null;
  max_dd_pct: number | null;
  sharpe: number | null;
  trades: number;
  win_pct: number | null;
  profit_factor: number | null;
  bh_pct: number | null;
  fees?: number | null;
  slippage?: number | null;
  cost_pct_of_capital?: number | null;
  exposure_pct?: number | null;
  ruin: boolean;
  costs?: Costs;
};

export type Trade = {
  side: 'long' | 'short';
  entry_ts: number;
  exit_ts: number;
  entry_px: number;
  exit_px: number;
  bars: number;
  reason: string;
  pnl: number;
  pnl_pct: number;
};

export type Window = { name: string; start: number; end: number };

export type Curve = { ts: number[]; equity: number[]; bh: number[] };

export type SweepRow = {
  params: Record<string, number>;
  train: Metrics;
  test: Metrics;
  train_rank: number;
  test_rank: number;
};

export type Job = {
  id: number;
  kind: 'backtest' | 'sweep';
  status: 'queued' | 'running' | 'done' | 'failed' | 'cancelled';
  progress: number | null;
  error: string | null;
  payload: Record<string, unknown>;
  createdAt: string;
  finishedAt: string | null;
  result: null | {
    strategy: string;
    symbol: string;
    timeframe: string;
    as_of: number;
    adjust: string;
    params?: Record<string, number>;
    windows: { train: Window; test: Window; holdout: Window; thin_sample: boolean };
    metrics?: { train: Metrics; test: Metrics; full: Metrics };
    runs?: { train: number; test: number; full: number };
    seconds: number;
    // sweeps
    combos?: number;
    evaluated?: number;
    reused?: number;
    top?: SweepRow[];
    rank_corr?: number | null;
    ranks?: [number, number][];
  };
  trades?: Trade[];
  curve?: Curve;
};
