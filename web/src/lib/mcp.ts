import { McpServer } from '@modelcontextprotocol/server';
import { z } from 'zod';

import { prisma } from './db';
import { CheckUnavailable, checkPine } from './pineCheck';
import { MAX_SOURCE_CHARS } from './pine';
import { PINE_REFERENCE } from './pineReference';
import { queueTest } from './queueTest';
import { listTests, testReport } from './tests';

/* The MCP server: what a user's own AI can do on their account.
 *
 * One fresh server per request (the SDK's stateless model), bound to the user
 * the bearer token belongs to -- every tool reads and writes only that user's
 * tests. Six tools, in the order a model uses them:
 *
 *   get_reference   the Pine contract (btb/pine/REFERENCE.md)
 *   list_markets    what can be tested, on which timeframes, over how long
 *   check_pine      the engine's own checker: errors with line and column
 *   run_test        queue a test exactly as the Strategies tab does
 *   get_test        wait for it (up to 50 s a call), then the score and why
 *   list_tests      the user's tests, to compare attempts
 *
 * Results are JSON text: models read it well, and it is what the pages show.
 */

const WAIT_MAX_S = 50;
const POLL_MS = 1500;

const text = (value: unknown) => ({
  content: [{ type: 'text' as const, text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }],
});
const fail = (message: string) => ({ ...text(message), isError: true });

export function buildServer(userId: string, origin: string) {
  const server = new McpServer({ name: 'beyond-the-backtest', version: '1.0.0' }, {
    instructions: 'Beyond the Backtest scores trading strategies for overfitting. Read get_reference '
      + 'first; write Pine v6 with // @grid lines; call check_pine until it returns ok; then run_test '
      + 'and get_test. Report the score, its three factors, the test return against buy-and-hold, '
      + 'and the link.',
  });

  server.registerTool('get_reference', {
    title: 'Pine contract',
    description: 'The rules a strategy must follow: Pine v6, inputs and // @grid pragmas, costs, '
      + 'data limits, patterns that work, a complete example, and how a test is judged. Read this first.',
    annotations: { readOnlyHint: true },
  }, async () => text(PINE_REFERENCE));

  server.registerTool('list_markets', {
    title: 'Markets',
    description: 'Every market that can be tested, with its timeframes, bar counts and history span.',
    inputSchema: z.object({
      asset_class: z.enum(['crypto', 'equity']).optional().describe('Only this asset class'),
    }),
    annotations: { readOnlyHint: true },
  }, async ({ asset_class }) => text(await markets(asset_class)));

  server.registerTool('check_pine', {
    title: 'Check a Pine script',
    description: 'Check a Pine v6 strategy with the same checker a test is held to. Returns ok, or errors '
      + 'with line and column; plus warnings, the inputs, the grid and its number of combinations, and '
      + 'the costs it will be charged. Fix every error and check again before run_test.',
    inputSchema: z.object({ source: z.string().max(MAX_SOURCE_CHARS).describe('The whole Pine script') }),
    annotations: { readOnlyHint: true },
  }, async ({ source }) => {
    try {
      const r = await checkPine(source, userId);
      const { spec: _s, name: _n, ...shown } = r;
      return text(shown);
    } catch (e) {
      return fail((e as CheckUnavailable).message);
    }
  });

  server.registerTool('run_test', {
    title: 'Run a test',
    description: 'Queue a backtest and its sweep for a Pine script (or a library strategy by name) on '
      + 'one market and timeframe. Returns a test_id and a link; then call get_test. A script with no '
      + '// @grid lines runs once and is not scored.',
    inputSchema: z.object({
      source: z.string().max(MAX_SOURCE_CHARS).optional().describe('A Pine v6 script'),
      strategy: z.string().optional().describe('Or the name of a library strategy'),
      symbol: z.string().describe('A market from list_markets, e.g. BTC/USD or SPY'),
      timeframe: z.string().describe('One of the market\'s timeframes, e.g. 4h or 1d'),
    }),
  }, async ({ source, strategy, symbol, timeframe }) => {
    if (!source === !strategy) return fail('Give exactly one of source or strategy');
    const out = await queueTest(userId, { kind: 'backtest', withSweep: true, symbol, timeframe, via: 'mcp',
                                          ...(source ? { source } : { strategy }) });
    if (!out.ok) return fail(out.error);
    return text({ test_id: out.id, status: 'queued', url: `${origin}/?test=${out.id}`,
                  next: 'Call get_test with this test_id until status is done.' });
  });

  server.registerTool('get_test', {
    title: 'Get a test',
    description: 'A test\'s status, and once done: the 0-100 score, its band and reason, the three '
      + 'factors (deflated Sharpe, 1 - PBO, walk-forward), the test, train and holdout returns against '
      + 'buy-and-hold, the costs charged, the top settings, and a link. Waits up to wait_seconds for it '
      + 'to finish.',
    inputSchema: z.object({
      test_id: z.number().int().positive(),
      wait_seconds: z.number().int().min(0).max(WAIT_MAX_S).optional()
        .describe(`How long to wait for it to finish, up to ${WAIT_MAX_S}; default ${WAIT_MAX_S}`),
    }),
    annotations: { readOnlyHint: true },
  }, async ({ test_id, wait_seconds }) => {
    const until = Date.now() + 1000 * (wait_seconds ?? WAIT_MAX_S);
    for (;;) {
      const r = await testReport(userId, test_id, origin);
      if (!r) return fail(`No test ${test_id} on this account`);
      if (r.status === 'done' || r.status === 'failed' || Date.now() + POLL_MS > until) return text(r);
      await new Promise((res) => setTimeout(res, POLL_MS));
    }
  });

  server.registerTool('list_tests', {
    title: 'List tests',
    description: 'This account\'s tests, newest first: strategy, market, score, test return against '
      + 'buy-and-hold, and the test_id for get_test.',
    inputSchema: z.object({
      symbol: z.string().optional(),
      min_score: z.number().min(0).max(100).optional(),
      limit: z.number().int().min(1).max(50).optional(),
    }),
    annotations: { readOnlyHint: true },
  }, async ({ symbol, min_score, limit }) => {
    const all = await listTests(userId);
    const rows = all
      .filter((t) => (!symbol || t.symbol === symbol) && (min_score == null || (t.score ?? -1) >= min_score))
      .slice(0, limit ?? 20)
      .map((t) => ({
        test_id: t.id, strategy: t.strategy, symbol: t.symbol, timeframe: t.timeframe,
        score: t.score, band: t.band, scored: !t.unscored, scoring: t.scoring,
        test_return_pct: t.testPct, buy_and_hold_pct: t.bhPct, settings_tried: t.combos,
        zero_costs: t.zeroCosts, created_at: t.createdAt, url: `${origin}/?test=${t.id}`,
      }));
    return text(rows);
  });

  server.registerPrompt('write_strategy', {
    title: 'Write and test a strategy',
    description: 'Turn an idea into a Pine strategy, check it, test it and report the score.',
    argsSchema: z.object({
      idea: z.string().describe('The strategy, in plain English'),
      market: z.string().optional().describe('e.g. BTC/USD 4h'),
    }),
  }, async ({ idea, market }) => ({
    messages: [{
      role: 'user' as const,
      content: {
        type: 'text' as const,
        text: `${PINE_REFERENCE}\n\n---\n\nWrite a Pine v6 strategy for this idea, following every rule above, `
          + `with // @grid lines for the settings worth testing. Check it with check_pine until it returns ok, `
          + `run it with run_test${market ? ` on ${market}` : ''}, wait with get_test, and report the score, `
          + `its three factors, the test return against buy-and-hold, and the link.\n\nIdea: ${idea}`,
      },
    }],
  }));

  server.registerResource('reference', 'btb://reference', {
    title: 'Pine contract', description: 'The rules a strategy must follow', mimeType: 'text/markdown',
  }, async (uri) => ({ contents: [{ uri: uri.href, mimeType: 'text/markdown', text: PINE_REFERENCE }] }));

  return server;
}

async function markets(assetClass?: string) {
  const [universe, series] = await Promise.all([
    prisma.universe.findMany({
      where: { active: true, ...(assetClass ? { asset_class: assetClass } : {}) },
      orderBy: [{ asset_class: 'asc' }, { symbol: 'asc' }],
    }),
    prisma.series.findMany({ select: { symbol: true, timeframe: true, row_count: true, first_ts: true, last_ts: true } }),
  ]);
  const day = (ts: bigint | null) => (ts == null ? null : new Date(Number(ts) * 1000).toISOString().slice(0, 10));
  return universe.map((u) => ({
    symbol: u.symbol, name: u.name, asset_class: u.asset_class,
    timeframes: series.filter((s) => s.symbol === u.symbol && s.row_count > 0).map((s) => ({
      timeframe: s.timeframe, bars: s.row_count, from: day(s.first_ts), to: day(s.last_ts),
      thin: s.row_count < 2000,
    })),
  })).filter((m) => m.timeframes.length > 0);
}
