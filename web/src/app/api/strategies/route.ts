import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';
import { comboCount } from '../../../lib/strategies';
import type { StrategyInfo } from '../../../lib/types';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* The strategy library.
 *
 * Read from the `strategies` table, which the Python side fills from its JSON
 * files (`python -m btb.engine library --sync`, also run on worker start).
 * The app never reads the backend's filesystem.
 *
 * Several versions of one strategy can exist -- editing a spec adds a row
 * rather than rewriting one, so old runs stay attached to the spec that made
 * them. Only the newest version of each is offered.
 */
export async function GET() {
  if (!(await currentUser())) {
    return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  }

  const rows = await prisma.strategies.findMany({
    where: { user_id: null },
    orderBy: { created_at: 'desc' },
    select: { name: true, spec: true },
  });

  const seen = new Set<string>();
  const strategies: StrategyInfo[] = [];
  for (const r of rows) {
    if (seen.has(r.name)) continue;
    seen.add(r.name);
    const s = r.spec as Record<string, any>;
    strategies.push({
      name: r.name,
      title: s.title ?? r.name,
      description: s.description ?? '',
      style: s.style ?? 'other',
      timeframes: s.timeframes ?? [],
      defaults: s.defaults ?? {},
      labels: s.labels ?? {},
      combos: comboCount(s.grid ?? {}, s.defaults ?? {}, s.constraints ?? []),
    });
  }
  strategies.sort((a, b) => a.title.localeCompare(b.title));
  return NextResponse.json({ strategies });
}
