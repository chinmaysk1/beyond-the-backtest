/* Corporate-action adjustment, applied when data is READ.
 *
 * A direct port of `backend/btb/data/adjust.py`. The two implementations must
 * agree: if the chart shows a different split-adjusted price than the engine
 * backtests on, one of them is lying and there is no way to tell which from
 * looking at either.
 *
 * `bars` holds raw prints. Every adjusted view is derived here from the events
 * table, which is what keeps stored rows permanent -- when a symbol splits next
 * year, nothing on disk changes meaning.
 *
 *   "none"   raw prints -- what traded, at the price it traded
 *   "split"  split-adjusted. The default for charts and backtests.
 *   "total"  splits and dividends, i.e. total return. For benchmarks.
 *
 * Splits are applied before dividends, because the venue quotes dividend
 * amounts already split-adjusted; applying one against a raw close subtracts a
 * nonsense fraction.
 */

export type Bar = {
  ts: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  partial: boolean;
};

export type MarketEvent = {
  ts: number;
  kind: 'split' | 'dividend';
  ratio: number | null;
  amount: number | null;
};

export type AdjustMode = 'none' | 'split' | 'total';

const DAY = 86400;

/* Which bars fall strictly before a corporate action.
 *
 * An ex-date is stamped at the session open (13:30 or 14:30 UTC) while daily
 * bars are stored snapped to UTC midnight. Comparing those directly puts the
 * ex-date bar before its own event and adjusts one bar too many -- the ex-date
 * bar is already trading post-split. So daily-or-slower series compare at day
 * granularity; intraday series compare raw timestamps, where the session-open
 * stamp already separates the ex-date's bars correctly.
 */
function isPrior(barTs: number, eventTs: number, step: number): boolean {
  if (step >= DAY) return Math.floor(barTs / DAY) < Math.floor(eventTs / DAY);
  return barTs < eventTs;
}

export function tfSeconds(timeframe: string): number {
  const units: Record<string, number> = { m: 60, h: 3600, d: 86400, w: 604800 };
  const unit = timeframe.slice(-1);
  const n = Number(timeframe.slice(0, -1));
  if (!units[unit] || !Number.isFinite(n) || n <= 0) {
    throw new Error(`unrecognised timeframe ${timeframe}`);
  }
  return n * units[unit];
}

export function adjustBars(
  bars: Bar[],
  events: MarketEvent[],
  mode: AdjustMode,
  timeframe: string,
): Bar[] {
  if (mode === 'none' || bars.length === 0 || events.length === 0) return bars;

  const step = tfSeconds(timeframe);
  const priceFactor = new Float64Array(bars.length).fill(1);
  const volumeFactor = new Float64Array(bars.length).fill(1);

  // --- splits, newest first so the factors compound --------------------------
  const splits = events
    .filter((e) => e.kind === 'split' && (e.ratio ?? 0) > 0)
    .sort((a, b) => b.ts - a.ts);

  for (const s of splits) {
    const ratio = s.ratio as number;
    for (let i = 0; i < bars.length; i++) {
      if (isPrior(bars[i].ts, s.ts, step)) {
        priceFactor[i] *= 1 / ratio;
        volumeFactor[i] *= ratio;
      }
    }
  }

  if (mode === 'total') {
    // Dividends are computed against SPLIT-ADJUSTED closes, since that is the
    // basis the venue quotes the amounts on.
    const splitClose = bars.map((b, i) => b.close * priceFactor[i]);
    const dividends = events
      .filter((e) => e.kind === 'dividend' && (e.amount ?? 0) > 0)
      .sort((a, b) => b.ts - a.ts);

    for (const d of dividends) {
      const firstOnOrAfter = bars.findIndex((b) => !isPrior(b.ts, d.ts, step));
      if (firstOnOrAfter <= 0) continue; // event outside the window we hold
      const c = splitClose[firstOnOrAfter];
      const amt = d.amount as number;
      // A dividend at or above the close drives the factor to zero or negative.
      // That is bad event data, not a corporate action, and must never be
      // allowed to invert a price series.
      if (!(c > 0) || amt >= c) continue;
      const f = 1 - amt / c;
      for (let i = 0; i < bars.length; i++) {
        if (isPrior(bars[i].ts, d.ts, step)) priceFactor[i] *= f;
      }
    }
  }

  return bars.map((b, i) => ({
    ...b,
    open: b.open * priceFactor[i],
    high: b.high * priceFactor[i],
    low: b.low * priceFactor[i],
    close: b.close * priceFactor[i],
    volume: b.volume * volumeFactor[i],
  }));
}
