/* Moving the train and test windows, in the app.
 *
 * Mirrors btb/engine/windows.py. The engine is the authority: it snaps every
 * boundary to a real bar and re-checks all of this, so these only have to
 * agree with it -- a request refused here would have been refused there, just
 * later and after taking a queue slot.
 *
 *     |<-- excluded -->|<--- train --->|<--- test --->|<-- holdout -->|
 *     first bar        a               b              c              newest
 *
 * Three handles: train start (a), the boundary (b, test's first bar) and test
 * end (c). Train and test stay contiguous -- train ends on the bar before b --
 * so the payload is derived from those three numbers rather than chosen freely.
 * The holdout is never movable: c stops at the last bar before it.
 */

export const DAY = 86400;
// btb/engine/windows.py MIN_WINDOW_BARS: the least a moved window may hold.
export const MIN_WINDOW_BARS = 100;
export const TF_SECONDS: Record<string, number> = {
  '5m': 300, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, '1d': 86400,
};

/* The job payload's `windows`: unix seconds, inclusive. */
export type WindowSpans = { train: [number, number]; test: [number, number] };

/* Train start, test start, test end. */
export type Handles = [number, number, number];

/* What the engine says may be moved (result.movable). */
export type Movable = { start: number; end: number; bars: number; min_bars: number };

export function toSpans([a, b, c]: Handles): WindowSpans {
  return { train: [a, b - 1], test: [b, c] };
}

/* Where split() puts the holdout, from a series' first and newest bar. The
 * holdout's first bar is the first one after this instant. */
export function holdoutFrom(first: number, last: number): number {
  return last - Math.min(365 * DAY, (last - first) * 0.25);
}

/* Least span a moved window should cover, from the average bar spacing. A
 * little over the engine's bar minimum, so a window the handles allow is not
 * refused for being a few bars short where the market was closed. */
export function minSpan(m: Movable): number {
  const spacing = (m.end - m.start) / Math.max(m.bars - 1, 1);
  return m.min_bars * spacing * 1.03;
}

/* Can this series' windows be moved at all? Two minimum windows must fit. */
export function canMove(m: Movable | undefined | null): m is Movable {
  return !!m && m.bars >= 2 * m.min_bars + 2 && m.end - m.start > 2 * minSpan(m);
}

/* Move one handle to `t`, snapped to the bar grid and clamped so every window
 * keeps its minimum and test never passes the last bar before the holdout. */
export function moveHandle(h: Handles, which: 0 | 1 | 2, t: number, m: Movable, step: number): Handles {
  const span = minSpan(m);
  const snap = (v: number) => m.start + Math.round((v - m.start) / step) * step;
  const [a, b, c] = h;
  const lim: [number, number][] = [[m.start, b - span], [a + span, c - span], [b + span, m.end]];
  const [lo, hi] = lim[which];
  const v = Math.max(lo, Math.min(hi, snap(t)));
  const out: Handles = [a, b, c];
  out[which] = Math.round(v);
  return out;
}

export function sameHandles(x: Handles, y: Handles): boolean {
  return x[0] === y[0] && x[1] === y[1] && x[2] === y[2];
}

type Check = { ok: true; windows: WindowSpans } | { ok: false; error: string };

/* Validate a request's `windows` against the stored series, before queueing.
 * Same order and same field paths as the engine's errors. */
export function checkWindows(
  raw: unknown, series: { first: number; last: number; timeframe: string },
): Check {
  const bad = (error: string): Check => ({ ok: false, error });
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return bad('windows: must be an object with train and test');
  const extra = Object.keys(raw).find((k) => k !== 'train' && k !== 'test');
  if (extra) return bad(`windows.${extra}: unknown window; only train and test can be moved (the holdout never can)`);
  const out: Partial<WindowSpans> = {};
  for (const name of ['train', 'test'] as const) {
    const v = (raw as any)[name];
    if (!Array.isArray(v) || v.length !== 2) return bad(`windows.${name}: must be [start, end] in unix seconds`);
    for (let i = 0; i < 2; i++) {
      if (typeof v[i] !== 'number' || !Number.isSafeInteger(v[i])) {
        return bad(`windows.${name}[${i}]: must be a whole number of seconds`);
      }
    }
    if (v[0] > v[1]) return bad(`windows.${name}: start is after end`);
    if (v[0] < series.first) return bad(`windows.${name}[0]: before the first bar (${day(series.first)})`);
    out[name] = [v[0], v[1]];
  }
  const w = out as WindowSpans;
  const hold = holdoutFrom(series.first, series.last);
  if (w.test[1] > hold) return bad(`windows.test[1]: reaches into the holdout, which starts after ${day(hold)}`);
  if (w.train[1] >= w.test[0]) return bad('windows.test[0]: test must start after train ends');
  // A window of n bars spans at least n - 1 bar lengths; anything shorter
  // cannot hold the minimum. The engine counts the actual bars.
  const floor = (MIN_WINDOW_BARS - 1) * (TF_SECONDS[series.timeframe] ?? 0);
  for (const name of ['train', 'test'] as const) {
    if (w[name][1] - w[name][0] < floor) return bad(`windows.${name}: too short; a moved window needs at least ${MIN_WINDOW_BARS} bars`);
  }
  return { ok: true, windows: w };
}

function day(t: number): string {
  return new Date(t * 1000).toISOString().slice(0, 10);
}
