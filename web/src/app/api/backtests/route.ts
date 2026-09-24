import { createHash, randomUUID } from 'node:crypto';

import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';
import { checkSpec } from '../../../lib/specCheck';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* Queue a backtest -- and, by default, the sweep that finds its best settings.
 * Returns immediately with the job ids.
 *
 * The app never runs the engine. It inserts `jobs` rows; the Python worker
 * claims them. Everything checkable here is checked here, so a bad request
 * fails fast with a message instead of occupying the queue -- but the worker
 * re-validates all of it, because it is the side spending the CPU.
 *
 *   { kind: "backtest", withSweep: true, strategy | spec, symbol, timeframe, costs?, params? }
 *
 * `strategy` names a library strategy. `spec` is a user's own JSON strategy:
 * it is stored as a strategies row owned by the user (deduplicated by hash)
 * and the jobs refer to it by id, so a result stays attached to the exact spec
 * that produced it.
 *
 * Queue cap: at most MAX_OPEN jobs queued or running per user. A run with its
 * sweep is two; one more leaves room to re-run a chosen row. The worker is one
 * core on a shared box.
 */
const MAX_OPEN = 4;
const MAX_COMMISSION_PCT = 2;
const MAX_SLIPPAGE_BPS = 200;

const bad = (error: string, status = 400) => NextResponse.json({ error }, { status });

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return bad('Unauthorised', 401);

  let body: any;
  try {
    body = await req.json();
  } catch {
    return bad('Expected a JSON body');
  }

  const kind = body?.kind === 'sweep' ? 'sweep' : body?.kind === 'backtest' ? 'backtest' : null;
  const withSweep = kind === 'backtest' && body?.withSweep === true;
  const symbol = typeof body?.symbol === 'string' ? body.symbol : '';
  const timeframe = typeof body?.timeframe === 'string' ? body.timeframe : '';
  if (!kind || !symbol || !timeframe) return bad('kind, symbol and timeframe are required');

  // Only the curated universe, only timeframes it lists.
  const market = await prisma.universe.findUnique({ where: { symbol } });
  if (!market || !market.active || !market.timeframes.includes(timeframe)) {
    return bad('Unknown market or timeframe', 404);
  }

  // -- which strategy ------------------------------------------------------
  const payload: Record<string, unknown> = { symbol, timeframe };
  let defaults: Record<string, number>;
  if (body.spec !== undefined) {
    const check = checkSpec(body.spec);
    if (!check.ok) return bad(`Strategy: ${check.errors[0]}`);
    const spec = check.spec;
    const sha = createHash('sha256').update(canonical(spec)).digest('hex');
    const existing = await prisma.strategies.findFirst({
      where: { user_id: user.id, spec_sha: sha }, select: { id: true },
    });
    const id = existing?.id ?? randomUUID();
    if (!existing) {
      await prisma.strategies.create({
        data: { id, user_id: user.id, name: spec.name, spec: spec as any, spec_sha: sha },
      });
    }
    payload.strategy_id = id;
    payload.strategy = spec.name;
    defaults = spec.defaults ?? {};
  } else {
    const strategy = typeof body.strategy === 'string' ? body.strategy : '';
    const row = await prisma.strategies.findFirst({
      where: { name: strategy, user_id: null }, orderBy: { created_at: 'desc' }, select: { spec: true },
    });
    if (!row) return bad('Unknown strategy', 404);
    payload.strategy = strategy;
    defaults = ((row.spec as any)?.defaults ?? {}) as Record<string, number>;
  }

  // -- settings and costs ---------------------------------------------------
  if (body.params !== undefined) {
    if (kind === 'sweep') return bad('A sweep runs the strategy\'s own grid; params are not accepted');
    if (typeof body.params !== 'object' || body.params === null) return bad('params must be an object');
    const params: Record<string, number> = {};
    for (const [k, v] of Object.entries(body.params)) {
      if (!(k in defaults)) return bad(`Unknown setting "${k}"`);
      if (typeof v !== 'number' || !Number.isFinite(v)) return bad(`Setting "${k}" must be a number`);
      params[k] = v;
    }
    payload.params = params;
  }
  if (body.costs !== undefined) {
    const c = Number(body.costs?.commission_pct);
    const s = Number(body.costs?.slippage_bps);
    if (!Number.isFinite(c) || !Number.isFinite(s)
        || c < 0 || c > MAX_COMMISSION_PCT || s < 0 || s > MAX_SLIPPAGE_BPS) {
      return bad(`Commission must be 0–${MAX_COMMISSION_PCT}% and slippage 0–${MAX_SLIPPAGE_BPS} bps`);
    }
    payload.costs = { commission_pct: c, slippage_bps: s };
  }

  const need = withSweep ? 2 : 1;
  const open = await prisma.jobs.count({
    where: { user_id: user.id, status: { in: ['queued', 'running'] } },
  });
  if (open + need > MAX_OPEN) {
    return bad(`You already have ${open} jobs running; wait for one to finish`, 429);
  }

  const job = await prisma.jobs.create({
    data: { user_id: user.id, kind, payload: payload as any, priority: kind === 'backtest' ? 50 : 100 },
    select: { id: true },
  });
  let sweepId: number | null = null;
  if (withSweep) {
    const { params: _unused, ...sweepPayload } = payload;
    const sweep = await prisma.jobs.create({
      data: { user_id: user.id, kind: 'sweep', payload: sweepPayload as any, priority: 60 },
      select: { id: true },
    });
    sweepId = Number(sweep.id);
  }
  return NextResponse.json({ id: Number(job.id), sweepId }, { status: 202 });
}

/* Sorted-key JSON, so the same spec pasted twice hashes the same. */
function canonical(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canonical).join(',')}]`;
  if (v && typeof v === 'object') {
    return `{${Object.keys(v as object).sort()
      .map((k) => `${JSON.stringify(k)}:${canonical((v as any)[k])}`).join(',')}}`;
  }
  return JSON.stringify(v);
}
