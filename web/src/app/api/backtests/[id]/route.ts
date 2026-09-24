import { NextResponse } from 'next/server';

import { currentUser } from '../../../../lib/auth';
import { prisma } from '../../../../lib/db';
import type { Job } from '../../../../lib/types';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* One job's status, and its result once done. The client polls this.
 *
 * A job is only visible to the user who queued it. For a finished backtest the
 * full-window run's trade list and equity curve are attached; they live on the
 * `runs` row rather than in the job result so a sweep's hundreds of rows do
 * not each carry a curve nobody draws.
 */
export async function GET(_req: Request, ctx: { params: Promise<{ id: string }> }) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });

  const { id } = await ctx.params;
  if (!/^\d{1,18}$/.test(id)) return NextResponse.json({ error: 'Not found' }, { status: 404 });

  const job = await prisma.jobs.findFirst({ where: { id: BigInt(id), user_id: user.id } });
  if (!job) return NextResponse.json({ error: 'Not found' }, { status: 404 });

  const out: Job = {
    id: Number(job.id),
    kind: job.kind as Job['kind'],
    status: job.status as Job['status'],
    progress: job.progress,
    error: job.error,
    payload: job.payload as Record<string, unknown>,
    createdAt: job.created_at.toISOString(),
    finishedAt: job.finished_at?.toISOString() ?? null,
    result: (job.result as Job['result']) ?? null,
  };

  const fullRun = out.result?.runs?.full;
  if (job.kind === 'backtest' && job.status === 'done' && fullRun) {
    const run = await prisma.runs.findUnique({
      where: { id: BigInt(fullRun) },
      select: { trades: true, equity: true },
    });
    out.trades = (run?.trades as Job['trades']) ?? [];
    out.curve = (run?.equity as Job['curve']) ?? undefined;
  }
  return NextResponse.json(out);
}
