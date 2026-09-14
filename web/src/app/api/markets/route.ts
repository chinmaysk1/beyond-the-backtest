import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* The curated universe, joined to what is actually stored for each symbol.
 *
 * `universe` says what MAY be fetched; `series` says what we HAVE. They are not
 * the same thing, and showing the first as though it were the second is how a
 * dashboard ends up claiming data it does not hold.
 */
export async function GET() {
  if (!(await currentUser())) {
    return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  }

  const [universe, series] = await Promise.all([
    prisma.universe.findMany({
      where: { active: true },
      orderBy: [{ asset_class: 'asc' }, { symbol: 'asc' }],
    }),
    prisma.series.findMany({
      select: {
        symbol: true, timeframe: true, source: true, asset_class: true,
        row_count: true, first_ts: true, last_ts: true, fetched_at: true,
      },
    }),
  ]);

  const bySymbol = new Map<string, typeof series>();
  for (const s of series) {
    const list = bySymbol.get(s.symbol) ?? [];
    list.push(s);
    bySymbol.set(s.symbol, list);
  }

  const order = ['5m', '15m', '30m', '1h', '4h', '12h', '1d', '1w'];

  const markets = universe
    .map((u) => {
      const stored = (bySymbol.get(u.symbol) ?? [])
        .filter((s) => s.row_count > 0)
        .sort((a, b) => order.indexOf(a.timeframe) - order.indexOf(b.timeframe));
      return {
        symbol: u.symbol,
        name: u.name,
        sector: u.sector,
        assetClass: u.asset_class,
        source: stored[0]?.source ?? null,
        bars: stored.reduce((n, s) => n + s.row_count, 0),
        firstTs: stored.length
          ? Number(stored.reduce((m, s) => (s.first_ts! < m ? s.first_ts! : m), stored[0].first_ts!))
          : null,
        timeframes: stored.map((s) => ({
          tf: s.timeframe,
          bars: s.row_count,
          firstTs: s.first_ts === null ? null : Number(s.first_ts),
          lastTs: s.last_ts === null ? null : Number(s.last_ts),
        })),
      };
    })
    // A symbol registered in the universe but not yet ingested is not a market
    // the user can look at. Hide it rather than offer an empty chart.
    .filter((m) => m.timeframes.length > 0);

  return NextResponse.json({ markets });
}
