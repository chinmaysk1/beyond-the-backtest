import { createHash, randomUUID } from 'node:crypto';

import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';
import { checkSpec } from '../../../lib/specCheck';
import type { TestSummary, Verdict } from '../../../lib/types';
import { checkWindows } from '../../../lib/windows';

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
 *   { kind: "backtest", withSweep: true, strategy | spec | strategyId, symbol, timeframe, costs?, params?, windows? }
 *
 * `strategy` names a library strategy. `spec` is a user's own JSON strategy:
 * it is stored as a strategies row owned by the user (deduplicated by hash)
 * and the jobs refer to it by id, so a result stays attached to the exact spec
 * that produced it. `strategyId` names one of those stored rows again.
 *
 * `windows` moves train and test: { train: [start, end], test: [start, end] },
 * unix seconds. It goes to both jobs, because the sweep ranks on the test
 * window and a ranking from other windows would not describe this run. Checked
 * here against the stored series -- inside it, ordered, clear of the holdout --
 * and again, to the bar, by the engine (lib/windows.ts, btb/engine/windows.py).
 *
 * Queue cap: at most MAX_OPEN jobs queued or running per user. A run with its
 * sweep is two; one more leaves room to re-run a chosen row. The worker is one
 * core on a shared box.
 */
const MAX_OPEN = 4;
const MAX_COMMISSION_PCT = 2;
const MAX_SLIPPAGE_BPS = 200;

const bad = (error: string, status = 400) => NextResponse.json({ error }, { status });

/* The Results tab: this user's tests, newest first, each with its verdict.
 *
 * A test is a finished backtest queued with its sweep. Re-runs made from a
 * test's page carry `of` (and a chosen row's carry params), so they are left
 * out -- they are a look at a test, not a new one. The sweep names its
 * backtest in its payload; sweeps queued before that pair by being the next
 * job, which is how they were always created.
 */
const LISTED = 50;
const SPARK = 40;

export async function GET() {
  const user = await currentUser();
  if (!user) return bad('Unauthorised', 401);

  const tests = (await prisma.jobs.findMany({
    where: { user_id: user.id, kind: 'backtest', status: 'done' },
    orderBy: { id: 'desc' },
    take: LISTED * 3,
    select: { id: true, payload: true, result: true, created_at: true },
  })).filter((j) => !(j.payload as any)?.params && (j.payload as any)?.of == null).slice(0, LISTED);
  if (!tests.length) return NextResponse.json({ tests: [] });

  const lo = tests[tests.length - 1].id;
  const sweeps = await prisma.jobs.findMany({
    where: { user_id: user.id, kind: 'sweep', id: { gt: lo } },
    select: { id: true, status: true, payload: true, result: true },
  });
  const byBacktest = new Map<bigint, (typeof sweeps)[number]>();
  for (const s of sweeps) {
    const of = (s.payload as any)?.backtest;
    if (typeof of === 'number') byBacktest.set(BigInt(of), s);
  }
  const byId = new Map(sweeps.map((s) => [s.id, s]));

  const fullRuns = tests.map((t) => (t.result as any)?.runs?.full).filter((x) => x != null);
  const curves = new Map((await prisma.runs.findMany({
    where: { id: { in: fullRuns.map((x) => BigInt(x)) } },
    select: { id: true, equity: true },
  })).map((r) => [Number(r.id), (r.equity as any)?.equity as number[] | undefined]));

  // Titles, not file names: the library's by name, a user's own by id.
  const pay = tests.map((t) => t.payload as any);
  const specs = await prisma.strategies.findMany({
    where: { OR: [
      { id: { in: pay.map((p) => p.strategy_id).filter(Boolean) }, user_id: user.id },
      { name: { in: pay.map((p) => p.strategy).filter(Boolean) }, user_id: null },
    ] },
    orderBy: { created_at: 'desc' },
    select: { id: true, name: true, user_id: true, spec: true },
  });
  const titleOf = (p: any): string | undefined => {
    const s = p.strategy_id ? specs.find((x) => x.id === p.strategy_id)
      : specs.find((x) => x.user_id === null && x.name === p.strategy);
    return (s?.spec as any)?.title ?? s?.name;
  };

  const out: TestSummary[] = [];
  for (const t of tests) {
    const r = t.result as any;
    const p = t.payload as any;
    const sw = byBacktest.get(t.id) ?? (() => {
      const next = byId.get(t.id + BigInt(1));
      const q = next?.payload as any;
      return q && q.backtest == null && q.symbol === p.symbol && q.timeframe === p.timeframe
        && (q.strategy_id ?? q.strategy) === (p.strategy_id ?? p.strategy) ? next : undefined;
    })();
    if (!sw) continue;
    const v = (sw.result as any)?.verdict as Verdict | undefined;
    const d = r?.default_windows;
    out.push({
      id: Number(t.id),
      sweepId: Number(sw.id),
      strategy: titleOf(p) ?? r?.strategy ?? p.strategy ?? 'Custom strategy',
      symbol: r?.symbol ?? p.symbol,
      timeframe: r?.timeframe ?? p.timeframe,
      createdAt: t.created_at.toISOString(),
      testPct: r?.metrics?.test?.net_pct ?? null,
      bhPct: r?.metrics?.test?.bh_pct ?? null,
      combos: (sw.result as any)?.combos ?? null,
      label: sw.status === 'done' ? v?.label ?? null : null,
      scoring: sw.status === 'queued' || sw.status === 'running',
      custom: !!d && JSON.stringify(d.train) + JSON.stringify(d.test)
        !== JSON.stringify(r.windows?.train) + JSON.stringify(r.windows?.test),
      spark: thin(curves.get(r?.runs?.full) ?? [], SPARK),
    });
  }
  return NextResponse.json({ tests: out });
}

function thin(xs: number[], n: number): number[] {
  if (xs.length <= n) return xs;
  return Array.from({ length: n }, (_, i) => xs[Math.round((i * (xs.length - 1)) / (n - 1))]);
}

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
  } else if (typeof body.strategyId === 'string') {
    // A stored spec of this user's, by id: how a test page re-runs a custom
    // strategy without holding its JSON.
    const row = await prisma.strategies.findFirst({
      where: { id: body.strategyId, user_id: user.id }, select: { id: true, name: true, spec: true },
    }).catch(() => null);
    if (!row) return bad('Unknown strategy', 404);
    payload.strategy_id = row.id;
    payload.strategy = row.name;
    defaults = ((row.spec as any)?.defaults ?? {}) as Record<string, number>;
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
  if (body.windows !== undefined && body.windows !== null) {
    const meta = await prisma.series.findFirst({
      where: { symbol, timeframe }, orderBy: { row_count: 'desc' },
      select: { first_ts: true, last_ts: true },
    });
    if (!meta?.first_ts || !meta.last_ts) return bad('No stored bars for that market and timeframe', 404);
    const check = checkWindows(body.windows, {
      first: Number(meta.first_ts), last: Number(meta.last_ts), timeframe,
    });
    if (!check.ok) return bad(check.error);
    payload.windows = check.windows;
  }
  // A re-run made from a test's page names that test, so it stays a view of
  // it rather than a new row in Results.
  if (Number.isSafeInteger(body.of) && body.of > 0) payload.of = body.of;

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
    // The sweep names its backtest, so the Results tab can pair them later.
    const { params: _unused, ...sweepPayload } = payload;
    sweepPayload.backtest = Number(job.id);
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
