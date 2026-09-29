import { NextResponse } from 'next/server';

import { currentUser } from '../../../../lib/auth';
import { prisma } from '../../../../lib/db';
import { strategyInfo } from '../../../../lib/strategies';
import type { CustomStrategyInfo } from '../../../../lib/types';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* The JSON strategies this user has run before, newest use first.
 *
 * POST /api/backtests already stores each pasted spec as a strategies row
 * owned by the user, deduplicated by hash -- so one row is one distinct spec,
 * however many times it ran. The row's own time is its first run; the order
 * here comes from its jobs, so a spec run again today rises to the top.
 */
const MAX_SHOWN = 50;
const JOBS_SCANNED = 500;

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });

  const [rows, jobs] = await Promise.all([
    prisma.strategies.findMany({
      where: { user_id: user.id },
      select: { id: true, name: true, spec: true, created_at: true },
    }),
    prisma.jobs.findMany({
      where: { user_id: user.id, kind: 'backtest' },
      orderBy: { created_at: 'desc' },
      take: JOBS_SCANNED,
      select: { payload: true, created_at: true },
    }),
  ]);

  const lastRun = new Map<string, Date>();
  for (const j of jobs) {
    const id = (j.payload as Record<string, any> | null)?.strategy_id;
    if (typeof id === 'string' && !lastRun.has(id)) lastRun.set(id, j.created_at);
  }

  const strategies: CustomStrategyInfo[] = rows
    .map((r) => ({
      ...strategyInfo(r.name, r.spec as Record<string, any>),
      id: r.id,
      spec: r.spec as Record<string, any>,
      lastRun: (lastRun.get(r.id) ?? r.created_at).toISOString(),
    }))
    .sort((a, b) => b.lastRun.localeCompare(a.lastRun))
    .slice(0, MAX_SHOWN);
  return NextResponse.json({ strategies });
}
