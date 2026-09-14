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
