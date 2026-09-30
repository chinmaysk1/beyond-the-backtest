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

/* A JSON strategy the user has run before: one per distinct spec. */
export type CustomStrategyInfo = StrategyInfo & {
  id: string;
  spec: Record<string, any>;
  lastRun: string;         // ISO time of its newest job
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

export type Split = { train: Window; test: Window; holdout: Window; thin_sample: boolean };

export type Curve = { ts: number[]; equity: number[]; bh: number[] };

export type SweepRow = {
  params: Record<string, number>;
  train: Metrics;
  test: Metrics;
  train_rank: number;
  test_rank: number;
};

/* btb/verdict: one word for a sweep, and why. Always scored on the default
 * windows, whatever the handles say. */
export type VerdictLabel = 'ROBUST' | 'WEAK' | 'OVERFIT';
export type CheckStatus = 'pass' | 'warn' | 'fail' | 'na';

export type Verdict = {
  label: VerdictLabel;
  reason: string;
  checks: {
    walk_forward: { status: CheckStatus; oos_pct?: number; bh_pct?: number | null; folds?: number;
                    folds_positive?: number; fold_pct?: number[]; pick_changed?: number;
                    pick_params?: Record<string, number>[] };
    pbo: { status: CheckStatus; value?: number; blocks?: number; splits?: number };
    dsr: { status: CheckStatus; value: number | null; trials: number; sharpe?: number; sr0?: number };
    sample: { status: CheckStatus; years: number };
    trades: { status: CheckStatus; count: number };
  };
  // Keyed by test rank on the default windows.
  rows: Record<string, { label: VerdictLabel; reason: string; dsr: number | null; params: Record<string, number> }>;
  combos: number;
  days: number;
  pbo_logits: number[] | null;      // 20 bins over [-4, 4]
  windows: Split;
  version: number;
};

/* One row of the Results tab: a test and its verdict. */
export type TestSummary = {
  id: number;
  sweepId: number | null;
  strategy: string;
  symbol: string;
  timeframe: string;
  createdAt: string;
  testPct: number | null;
  bhPct: number | null;
  combos: number | null;
  label: VerdictLabel | null;          // null: no verdict (still scoring, failed, or from before it)
  scoring: boolean;
  custom: boolean;                     // run on moved windows
  spark: number[];                     // equity over train + test, ~40 points
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
    windows: Split;
    // The windows the engine would have used, and what may be moved. Absent
    // on results from before windows could be moved.
    default_windows?: Split;
    movable?: { start: number; end: number; bars: number; min_bars: number };
    metrics?: { train: Metrics; test: Metrics; full: Metrics };
    runs?: { train: number; test: number; full: number };
    seconds: number;
    // sweeps
    combos?: number;
    evaluated?: number;
    reused?: number;
    top?: SweepRow[];
    rows?: SweepRow[];               // every combination, test-ranked (absent on older sweeps)
    rank_corr?: number | null;
    ranks?: [number, number][];
    verdict?: Verdict;
  };
  trades?: Trade[];
  curve?: Curve;
};
