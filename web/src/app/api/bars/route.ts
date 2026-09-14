import { NextResponse } from 'next/server';

import { adjustBars, type AdjustMode, type Bar, type MarketEvent } from '../../../lib/adjust';
import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const MAX_LIMIT = 2000;
const MODES: AdjustMode[] = ['none', 'split', 'total'];

/* Bars for one series, in the requested adjustment view.
 *
 * Three things this endpoint is careful about:
 *
 * 1. The symbol is checked against the `universe` table. A public app must
 *    never let a query string name an arbitrary series.
 * 2. The newest bar may be `partial` -- still forming, its close only the
 *    current price. It is returned, flagged, so the chart can draw it
 *    distinctly; it is never silently mixed in as a settled bar.
 * 3. Adjustment happens here, from the events table, because `bars` holds raw
 *    prints. Same code path as the Python engine (`btb/data/adjust.py`).
 */
export async function GET(req: Request) {
  if (!(await currentUser())) {
    return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  }

  const url = new URL(req.url);
  const symbol = url.searchParams.get('symbol') ?? '';
  const timeframe = url.searchParams.get('timeframe') ?? '1d';
  const modeParam = (url.searchParams.get('adjust') ?? 'split') as AdjustMode;
  const mode = MODES.includes(modeParam) ? modeParam : 'split';
  const limit = Math.min(
    MAX_LIMIT,
    Math.max(50, Number(url.searchParams.get('limit') ?? 900) || 900),
  );
  const before = Number(url.searchParams.get('before') ?? 0) || null;

  // Only symbols in the curated universe. Not a formality: without it the
  // query string picks the table scan.
  const known = await prisma.universe.findUnique({ where: { symbol } });
  if (!known) {
    return NextResponse.json({ error: 'Unknown symbol' }, { status: 404 });
  }

  const meta = await prisma.series.findFirst({
    where: { symbol, timeframe },
    select: { source: true, first_ts: true, last_ts: true, row_count: true },
  });
  if (!meta) {
    return NextResponse.json({ error: 'No data for that timeframe' }, { status: 404 });
  }

  /* Paging backwards: take the newest `limit` bars at or before `before`, then
   * put them back in ascending order. Ordering descending in SQL and reversing
   * in memory is what makes this an index scan of `limit` rows rather than a
   * scan of the whole series -- which matters when one series is 315,000 bars. */
  const rows = await prisma.bars.findMany({
    where: {
      source: meta.source,
      symbol,
      timeframe,
      ...(before ? { ts: { lt: BigInt(before) } } : {}),
    },
    orderBy: { ts: 'desc' },
    take: limit,
  });
  rows.reverse();

  const bars: Bar[] = rows.map((r) => ({
    ts: Number(r.ts),
    open: r.open,
    high: r.high,
    low: r.low,
    close: r.close,
    volume: r.volume,
    partial: r.partial,
  }));

  let events: MarketEvent[] = [];
  if (mode !== 'none') {
    const ev = await prisma.events.findMany({
      where: { source: meta.source, symbol },
      orderBy: { ts: 'asc' },
    });
    events = ev.map((e) => ({
      ts: Number(e.ts),
      kind: e.kind as 'split' | 'dividend',
      ratio: e.ratio,
      amount: e.amount,
    }));
  }

  return NextResponse.json({
    symbol,
    timeframe,
    adjust: mode,
    source: meta.source,
    total: meta.row_count,
    firstTs: meta.first_ts === null ? null : Number(meta.first_ts),
    lastTs: meta.last_ts === null ? null : Number(meta.last_ts),
    splits: events.filter((e) => e.kind === 'split').length,
    dividends: events.filter((e) => e.kind === 'dividend').length,
    bars: adjustBars(bars, events, mode, timeframe),
  });
}
