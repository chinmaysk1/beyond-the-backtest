import { randomUUID } from 'node:crypto';

import { prisma } from './db';
import { CheckUnavailable, checkPine } from './pineCheck';
import { MAX_SOURCE_CHARS, messageText } from './pine';
import { checkWindows } from './windows';

/* Queueing a test: the one path behind POST /api/backtests and the MCP
 * `run_test` tool, so a test started from the page and one started by a
 * user's own AI are the same jobs, checked the same way.
 *
 *   { kind: "backtest", withSweep: true, strategy | source | strategyId, symbol, timeframe,
 *     costs?, params?, windows?, of?, extend? }
 *
 * `strategy` names a library strategy. `source` is a user's own Pine script: it
 * is checked (lib/pineCheck.ts), stored as a strategies row owned by the user
 * (deduplicated by source hash) and the jobs refer to it by id, so a result
 * stays attached to the exact script that produced it. `strategyId` names one
 * of those stored rows again.
 *
 * `extend` (a backtest, no sweep) brings one of the user's finished backtests up
 * to date: the worker reruns only its holdout on the newest bars and writes it
 * back onto that backtest. The worker checks it is the user's own.
 *
 * `windows` moves train and test: { train: [start, end], test: [start, end] },
 * unix seconds. It goes to both jobs, because the sweep ranks on the test
 * window and a ranking from other windows would not describe this run.
 *
 * `costs` are the defaults for a script that sets none of its own; a script
 * that sets any is charged exactly what it says.
 *
 * Queue cap: at most MAX_OPEN jobs queued or running per user. A run with its
 * sweep is two; one more leaves room to re-run a chosen row. The worker is one
 * shared box.
 */
export const MAX_OPEN = 4;
const MAX_COMMISSION_PCT = 2;
const MAX_SLIPPAGE_BPS = 200;

export type Queued = { ok: true; id: number; sweepId: number | null; strategyId?: string }
  | { ok: false; error: string; status: number };

const fail = (error: string, status = 400): Queued => ({ ok: false, error, status });

export async function queueTest(userId: string, body: any): Promise<Queued> {
  const kind = body?.kind === 'sweep' ? 'sweep' : body?.kind === 'backtest' ? 'backtest' : null;
  const withSweep = kind === 'backtest' && body?.withSweep === true;
  const symbol = typeof body?.symbol === 'string' ? body.symbol : '';
  const timeframe = typeof body?.timeframe === 'string' ? body.timeframe : '';
  if (!kind || !symbol || !timeframe) return fail('kind, symbol and timeframe are required');

  // Only the curated universe, only timeframes it lists.
  const market = await prisma.universe.findUnique({ where: { symbol } });
  if (!market || !market.active || !market.timeframes.includes(timeframe)) {
    return fail('Unknown market or timeframe', 404);
  }

  // -- which strategy ------------------------------------------------------
  const payload: Record<string, unknown> = { symbol, timeframe };
  let defaults: Record<string, number>;
  if (typeof body.source === 'string') {
    if (body.source.length > MAX_SOURCE_CHARS) return fail('The script is too long');
    let check;
    try {
      check = await checkPine(body.source, userId);
    } catch (e) {
      return fail((e as CheckUnavailable).message, 503);
    }
    if (!check.ok || !check.spec || !check.name) {
      return fail(`Strategy: ${check.errors[0] ? messageText(check.errors[0]) : 'does not check out'}`);
    }
    const existing = await prisma.strategies.findFirst({
      where: { user_id: userId, source_sha: check.sha, language: 'pine' }, select: { id: true },
    });
    const id = existing?.id ?? randomUUID();
    if (!existing) {
      await prisma.strategies.create({
        data: { id, user_id: userId, name: check.name, spec: check.spec as any, spec_sha: check.sha,
                language: 'pine', source: body.source, source_sha: check.sha },
      });
    }
    payload.strategy_id = id;
    payload.strategy = check.name;
    defaults = (check.spec.defaults ?? {}) as Record<string, number>;
  } else if (typeof body.strategyId === 'string') {
    // A stored script of this user's, by id: how a test page re-runs a custom
    // strategy without holding its source.
    const row = await prisma.strategies.findFirst({
      where: { id: body.strategyId, user_id: userId },
      select: { id: true, name: true, spec: true, language: true },
    }).catch(() => null);
    if (!row) return fail('Unknown strategy', 404);
    if (row.language !== 'pine') return fail('That strategy uses the retired JSON format and can’t be run again');
    payload.strategy_id = row.id;
    payload.strategy = row.name;
    defaults = ((row.spec as any)?.defaults ?? {}) as Record<string, number>;
  } else if (body.spec !== undefined) {
    return fail('JSON strategies are retired: send the strategy as Pine `source`');
  } else {
    const strategy = typeof body.strategy === 'string' ? body.strategy : '';
    const row = await prisma.strategies.findFirst({
      where: { name: strategy, user_id: null, language: 'pine' },
      orderBy: { created_at: 'desc' }, select: { spec: true },
    });
    if (!row) return fail('Unknown strategy', 404);
    payload.strategy = strategy;
    defaults = ((row.spec as any)?.defaults ?? {}) as Record<string, number>;
  }

  // -- settings and costs ---------------------------------------------------
  if (body.params !== undefined) {
    if (kind === 'sweep') return fail('A sweep runs the strategy\'s own grid; params are not accepted');
    if (typeof body.params !== 'object' || body.params === null) return fail('params must be an object');
    const params: Record<string, number> = {};
    for (const [k, v] of Object.entries(body.params)) {
      if (!(k in defaults)) return fail(`Unknown setting "${k}"`);
      if (typeof v !== 'number' || !Number.isFinite(v)) return fail(`Setting "${k}" must be a number`);
      params[k] = v;
    }
    payload.params = params;
  }
  if (body.costs !== undefined) {
    const c = Number(body.costs?.commission_pct);
    const s = Number(body.costs?.slippage_bps);
    if (!Number.isFinite(c) || !Number.isFinite(s)
        || c < 0 || c > MAX_COMMISSION_PCT || s < 0 || s > MAX_SLIPPAGE_BPS) {
      return fail(`Commission must be 0–${MAX_COMMISSION_PCT}% and slippage 0–${MAX_SLIPPAGE_BPS} bps`);
    }
    payload.costs = { commission_pct: c, slippage_bps: s };
  }
  if (body.windows !== undefined && body.windows !== null) {
    const meta = await prisma.series.findFirst({
      where: { symbol, timeframe }, orderBy: { row_count: 'desc' },
      select: { first_ts: true, last_ts: true },
    });
    if (!meta?.first_ts || !meta.last_ts) return fail('No stored bars for that market and timeframe', 404);
    const check = checkWindows(body.windows, {
      first: Number(meta.first_ts), last: Number(meta.last_ts), timeframe,
    });
    if (!check.ok) return fail(check.error);
    payload.windows = check.windows;
  }
  // A re-run made from a test's page names that test, so it stays a view of
  // it rather than a new row in Results.
  if (Number.isSafeInteger(body.of) && body.of > 0) payload.of = body.of;
  if (body.extend !== undefined) {
    if (kind !== 'backtest' || withSweep || !Number.isSafeInteger(body.extend) || body.extend < 1) {
      return fail('extend must be a backtest id, on a backtest without a sweep');
    }
    payload.extend = body.extend;
  }
  if (typeof body.via === 'string') payload.via = body.via.slice(0, 40);

  const need = withSweep ? 2 : 1;
  const open = await prisma.jobs.count({
    where: { user_id: userId, status: { in: ['queued', 'running'] }, kind: { not: 'check' } },
  });
  if (open + need > MAX_OPEN) {
    return fail(`You already have ${open} jobs running; wait for one to finish`, 429);
  }

  const job = await prisma.jobs.create({
    data: { user_id: userId, kind, payload: payload as any, priority: kind === 'backtest' ? 50 : 100 },
    select: { id: true },
  });
  let sweepId: number | null = null;
  if (withSweep) {
    // The sweep names its backtest, so the Results tab can pair them later.
    const { params: _unused, ...sweepPayload } = payload;
    sweepPayload.backtest = Number(job.id);
    const sweep = await prisma.jobs.create({
      data: { user_id: userId, kind: 'sweep', payload: sweepPayload as any, priority: 60 },
      select: { id: true },
    });
    sweepId = Number(sweep.id);
  }
  return { ok: true, id: Number(job.id), sweepId,
           strategyId: typeof payload.strategy_id === 'string' ? payload.strategy_id : undefined };
}
