import { prisma } from './db';
import type { TestSummary, Verdict } from './types';

/* A user's tests, as the Results tab and the MCP `list_tests` tool read them.
 *
 * A test is a finished backtest queued with its sweep. Re-runs made from a
 * test's page carry `of` (and a chosen row's carry params), so they are left
 * out -- they are a look at a test, not a new one. The sweep names its
 * backtest in its payload; sweeps queued before that pair by being the next
 * job, which is how they were always created.
 *
 * One card per strategy, market and timeframe: running the same test again
 * replaces its card with the newer run rather than adding a second one. A
 * strategy is known by its title, so an edited custom strategy (a new stored
 * version under the same name) also replaces its older card.
 */
const LISTED = 50;
const SPARK = 40;

export async function listTests(userId: string): Promise<TestSummary[]> {
  const tests = (await prisma.jobs.findMany({
    where: { user_id: userId, kind: 'backtest', status: 'done' },
    orderBy: { id: 'desc' },
    take: LISTED * 6,
    select: { id: true, payload: true, result: true, created_at: true },
  })).filter((j) => !(j.payload as any)?.params && (j.payload as any)?.of == null)
    .filter(newestOf((j) => { const p = j.payload as any; return `${p.strategy_id ?? p.strategy}|${p.symbol}|${p.timeframe}`; }))
    .slice(0, LISTED);
  if (!tests.length) return [];

  const lo = tests[tests.length - 1].id;
  const sweeps = await prisma.jobs.findMany({
    where: { user_id: userId, kind: 'sweep', id: { gt: lo } },
    select: { id: true, status: true, payload: true, result: true },
  });
  const byBacktest = new Map<bigint, (typeof sweeps)[number]>();
  for (const s of sweeps) {
    const of = (s.payload as any)?.backtest;
    if (typeof of === 'number') byBacktest.set(BigInt(of), s);
  }
  const byId = new Map(sweeps.map((s) => [s.id, s]));

  const drawn = (r: any) => r?.runs?.all ?? r?.runs?.full;
  const fullRuns = tests.map((t) => drawn(t.result as any)).filter((x) => x != null);
  type Curve = { ts?: number[]; equity?: number[]; bh?: number[] };
  const curves = new Map((await prisma.runs.findMany({
    where: { id: { in: fullRuns.map((x) => BigInt(x)) } },
    select: { id: true, equity: true },
  })).map((r) => [Number(r.id), (r.equity ?? {}) as Curve]));

  // Titles, not file names: the library's by name, a user's own by id. The
  // library's newest version is its Pine one; a test made with the retired
  // JSON library still finds its own row by the language it ran in.
  const pay = tests.map((t) => t.payload as any);
  const specs = await prisma.strategies.findMany({
    where: { OR: [
      { id: { in: pay.map((p) => p.strategy_id).filter(Boolean) }, user_id: userId },
      { name: { in: pay.map((p) => p.strategy).filter(Boolean) }, user_id: null },
    ] },
    orderBy: { created_at: 'desc' },
    select: { id: true, name: true, user_id: true, spec: true, language: true },
  });
  const specOf = (p: any, lang: string) => p.strategy_id ? specs.find((x) => x.id === p.strategy_id)
    : specs.find((x) => x.user_id === null && x.name === p.strategy && x.language === lang)
      ?? specs.find((x) => x.user_id === null && x.name === p.strategy);
  const titleOf = (p: any, lang: string): string | undefined => {
    const s = specOf(p, lang);
    return (s?.spec as any)?.title ?? s?.name;
  };

  const out: TestSummary[] = [];
  const shownKey = newestOf((k: string) => k);
  for (const t of tests) {
    const r = t.result as any;
    const p = t.payload as any;
    const lang = r?.language === 'pine' ? 'pine' : 'json';
    const sw = byBacktest.get(t.id) ?? (() => {
      const next = byId.get(t.id + BigInt(1));
      const q = next?.payload as any;
      return q && q.backtest == null && q.symbol === p.symbol && q.timeframe === p.timeframe
        && (q.strategy_id ?? q.strategy) === (p.strategy_id ?? p.strategy) ? next : undefined;
    })();
    if (!sw) continue;
    if (!shownKey(`${titleOf(p, lang) ?? p.strategy_id ?? p.strategy}|${p.symbol}|${p.timeframe}`)) continue;
    const sres = sw.result as any;
    const v = sres?.verdict as Verdict | undefined;
    const scored = sw.status === 'done' && typeof v?.score === 'number';
    const d = r?.default_windows;
    const test = r?.metrics?.test;
    out.push({
      id: Number(t.id),
      sweepId: Number(sw.id),
      strategy: titleOf(p, lang) ?? r?.strategy ?? p.strategy ?? 'Custom strategy',
      symbol: r?.symbol ?? p.symbol,
      timeframe: r?.timeframe ?? p.timeframe,
      createdAt: t.created_at.toISOString(),
      testPct: test?.net_pct ?? null,
      bhPct: test?.bh_pct ?? null,
      maxDd: test?.max_dd_pct ?? null,
      combos: sres?.combos ?? null,
      params: r?.params ?? {},
      labels: (specOf(p, lang)?.spec as any)?.labels ?? {},
      score: scored ? v!.score : null,
      band: scored ? v!.band : null,
      reason: scored ? v!.reason : (sres?.scored === false ? sres.reason ?? null : null),
      scoring: sw.status === 'queued' || sw.status === 'running',
      thin: !!r?.windows?.thin_sample || (v?.checks?.trades?.status === 'warn'),
      custom: !!d && JSON.stringify(d.train) + JSON.stringify(d.test)
        !== JSON.stringify(r.windows?.train) + JSON.stringify(r.windows?.test),
      spark: testSpark(curves.get(drawn(r)), r?.windows?.test?.start, r?.windows?.test?.end),
      unscored: sw.status === 'done' && sres?.scored === false,
      zeroCosts: !!r?.config?.costs?.zero,
      legacy: lang !== 'pine',
      via: typeof p.via === 'string' ? p.via : null,
    });
  }
  return out;
}

/* A filter that keeps the first item per key -- with newest-first input, the
 * newest run of each strategy / market / timeframe. */
function newestOf<T>(key: (x: T) => string) {
  const seen = new Set<string>();
  return (x: T) => { const k = key(x); if (seen.has(k)) return false; seen.add(k); return true; };
}

/* The card's curve: strategy and buy-and-hold over the test window only, both
 * rebased to 1, so the line ends where the card's numbers say it does. */
function testSpark(c: { ts?: number[]; equity?: number[]; bh?: number[] } | undefined, start?: number, end?: number) {
  const ts = c?.ts ?? [], eq = c?.equity ?? [], bh = c?.bh ?? [];
  const i0 = Math.max(0, start == null ? 0 : ts.findIndex((t) => t >= start));
  const after = end == null ? -1 : ts.findIndex((t) => t > end);
  const i1 = after < 0 ? ts.length : after;
  const cut = (xs: number[]) => {
    const ys = xs.slice(i0, i1);
    return ys.length && ys[0] ? thin(ys.map((y) => y / ys[0]), SPARK) : [];
  };
  return { equity: cut(eq), bh: cut(bh) };
}

function thin(xs: number[], n: number): number[] {
  if (xs.length <= n) return xs;
  return Array.from({ length: n }, (_, i) => xs[Math.round((i * (xs.length - 1)) / (n - 1))]);
}

/* One test, for a model to read: where it is, and when it is done, what it
 * found -- the score and its three factors, the test return against holding,
 * the holdout, the best settings. Plain numbers, no curves. */
export async function testReport(userId: string, backtestId: number, origin: string) {
  const bt = await prisma.jobs.findFirst({
    where: { id: BigInt(backtestId), user_id: userId, kind: 'backtest' },
    select: { id: true, status: true, error: true, progress: true, payload: true, result: true },
  });
  if (!bt) return null;
  const sw = await prisma.jobs.findFirst({
    where: { user_id: userId, kind: 'sweep', payload: { path: ['backtest'], equals: backtestId } },
    select: { id: true, status: true, error: true, progress: true, result: true },
  });
  const r = bt.result as any;
  const s = sw?.result as any;
  const url = `${origin}/?test=${backtestId}`;
  const status = bt.status === 'failed' || sw?.status === 'failed' ? 'failed'
    : bt.status === 'done' && (!sw || sw.status === 'done') ? 'done'
      : bt.status === 'queued' && (!sw || sw.status === 'queued') ? 'queued' : 'running';
  const base = { test_id: backtestId, status, url };
  if (status === 'failed') return { ...base, error: bt.error ?? sw?.error ?? 'the test failed' };
  if (status !== 'done') {
    return { ...base, progress: bt.status !== 'done' ? 0 : Math.round(100 * (sw?.progress ?? 0)) / 100 };
  }
  const m = r?.metrics ?? {};
  const pick = (x: any) => x && {
    return_pct: x.net_pct, buy_and_hold_pct: x.bh_pct, max_drawdown_pct: x.max_dd_pct,
    sharpe: x.sharpe, trades: x.trades, win_pct: x.win_pct,
  };
  const v = s?.verdict;
  return {
    ...base,
    strategy: r?.strategy, symbol: r?.symbol, timeframe: r?.timeframe,
    scored: s?.scored !== false && !!v,
    score: v?.score ?? null, band: v?.band ?? null, reason: v?.reason ?? s?.reason ?? null,
    factors: v?.factors ?? null,
    settings_tried: s?.combos ?? 1,
    windows: r?.windows,
    test: pick(m.test), train: pick(m.train), holdout: pick(m.holdout),
    costs: r?.config?.costs ?? null,
    best_settings: (s?.top ?? []).slice(0, 5).map((row: any) => ({
      params: row.params, test_rank: row.test_rank, train_rank: row.train_rank,
      test_return_pct: row.test?.net_pct, train_return_pct: row.train?.net_pct,
      score: v?.rows?.[String(row.test_rank)]?.score ?? null,
    })),
  };
}
