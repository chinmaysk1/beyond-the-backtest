/* A mirror of btb/engine/spec.py:validate(), for fast feedback.
 *
 * Used in the browser (live, as the user pastes) and in POST /api/backtests
 * (reject obvious mistakes before a job is queued). It is NOT the authority:
 * the engine re-validates every spec, with the full rules, before running it,
 * and a job that fails there comes back with the engine's exact error. Keep
 * the function and condition lists in step with spec.py's CATALOG.
 */

import { comboCount } from './strategies';

const FNS = new Set(('add sub mul div min max pow neg abs sqrt log round shift change roc highest lowest sum '
  + 'stdev barssince sma ema wma rma hma vwma atr tr rsi adx dmi macd bbands stoch cci mfi williams_r obv vwap '
  + 'donchian keltner supertrend psar').split(' '));
const CONDS = new Set('gt lt gte lte eq cross_above cross_below rising falling all any not between'.split(' '));
const SOURCES = new Set('open high low close volume hl2 hlc3 ohlc4'.split(' '));
const OPTIONS = new Set(['fn', 'out', 'tf', 'tf_mult']);
const RULES = ['entry_long', 'exit_long', 'entry_short', 'exit_short'];

/** Sweeps beyond this are refused: a grid that size is a search, not a test. */
export const MAX_COMBOS = 500;

export type SpecCheck =
  | { ok: true; spec: Record<string, any>; settings: number; combos: number }
  | { ok: false; errors: string[] };

export function checkSpecText(text: string): SpecCheck {
  let spec: unknown;
  try {
    spec = JSON.parse(text);
  } catch (e) {
    return { ok: false, errors: [`Not valid JSON: ${(e as Error).message.replace(/^JSON\.parse: /, '')}`] };
  }
  return checkSpec(spec);
}

export function checkSpec(s: any): SpecCheck {
  const errs: string[] = [];
  if (typeof s !== 'object' || !s || Array.isArray(s)) return { ok: false, errors: ['$: must be a JSON object'] };
  if (!/^[a-z][a-z0-9_]{0,63}$/.test(s.name ?? '')) errs.push('name: required, lowercase snake_case');
  const inds = (s.indicators ?? {}) as Record<string, unknown>;
  const defs = (s.defaults ?? {}) as Record<string, unknown>;
  const used = new Set<string>();

  const value = (n: any, p: string): void => {
    if (typeof n === 'number') return;
    if (typeof n === 'string') {
      if (n.startsWith('$')) used.add(n.slice(1));
      else if (!SOURCES.has(n) && !(n in inds)) errs.push(`${p}: unknown name '${n}'`);
      return;
    }
    if (!n || typeof n !== 'object' || !('fn' in n)) {
      errs.push(`${p}: expected a number, "$param", a name, or {"fn": ...}`);
      return;
    }
    if (!FNS.has(n.fn)) { errs.push(`${p}.fn: unknown function '${n.fn}'`); return; }
    for (const [k, v] of Object.entries(n)) if (!OPTIONS.has(k)) value(v, `${p}.${k}`);
  };
  const cond = (c: any, p: string): void => {
    if (typeof c === 'boolean') return;
    if (!c || typeof c !== 'object' || Array.isArray(c) || Object.keys(c).length !== 1) {
      errs.push(`${p}: a condition is one key, like {"gt": [a, b]}`);
      return;
    }
    const [op, a] = Object.entries(c)[0] as [string, any];
    if (!CONDS.has(op)) { errs.push(`${p}: unknown condition '${op}'`); return; }
    if (op === 'not') { cond(a, `${p}.not`); return; }
    if (!Array.isArray(a)) { errs.push(`${p}.${op}: expects a list`); return; }
    if (op === 'all' || op === 'any') { a.forEach((x, i) => cond(x, `${p}.${op}[${i}]`)); return; }
    a.forEach((x, i) => value(x, `${p}.${op}[${i}]`));
  };

  for (const [k, v] of Object.entries(inds)) value(v, `indicators.${k}`);
  const rules = RULES.filter((r) => r in s);
  if (!rules.some((r) => r.startsWith('entry'))) errs.push('entry_long / entry_short: at least one entry rule is required');
  rules.forEach((r) => cond(s[r], r));
  const params = (o: any): void => {
    if (typeof o === 'string' && o.startsWith('$')) used.add(o.slice(1));
    else if (o && typeof o === 'object') Object.values(o).forEach(params);
  };
  params(s.stop); params(s.take_profit); params(s.max_bars);
  for (const [k, v] of Object.entries(defs)) {
    if (typeof v !== 'number' || !Number.isFinite(v)) errs.push(`defaults.${k}: must be a number`);
  }
  used.forEach((p) => { if (!(p in defs)) errs.push(`defaults.${p}: $${p} is used but has no default`); });
  const grid = (s.grid ?? {}) as Record<string, unknown>;
  for (const [k, v] of Object.entries(grid)) {
    if (!(k in defs)) errs.push(`grid.${k}: not a parameter (add it to defaults)`);
    if (!Array.isArray(v) || !v.length || !v.every((x) => typeof x === 'number')) {
      errs.push(`grid.${k}: must be a non-empty list of numbers`);
    }
  }
  if (errs.length) return { ok: false, errors: errs };

  const combos = comboCount(grid as Record<string, number[]>, defs as Record<string, number>,
    (s.constraints ?? []) as string[]);
  if (combos > MAX_COMBOS) {
    return { ok: false, errors: [`grid: ${combos} combinations; at most ${MAX_COMBOS}. Use fewer, coarser values.`] };
  }
  return { ok: true, spec: s, settings: Object.keys(defs).length, combos: Math.max(combos, 1) };
}

/* What a user pastes into ChatGPT or Claude. Kept in step with the catalog. */
export const AI_PROMPT = `You are writing a trading strategy for "Beyond the Backtest".
Reply with ONE JSON object and nothing else.

Keys: name (snake_case), title, description, style (trend | breakout | mean-reversion | other),
indicators, entry_long, exit_long, entry_short, exit_short, stop, take_profit, max_bars,
defaults (a number for every $param), grid (3–5 coarse values per setting), constraints.

A value is: a number, "$param", a price (open high low close volume hl2 hlc3 ohlc4),
a name from "indicators", or {"fn": ...}. Any argument can itself be a {"fn": ...}.
Functions:
  sma ema wma rma hma vwma (src, len)      rsi (src, len)      atr adx (len)
  dmi (len, out: plus|minus|adx)           macd (src, fast, slow, signal, out: macd|signal|hist)
  bbands (src, len, mult, out: upper|mid|lower)   stoch (len, smooth_k, d, out: k|d)
  donchian (len, out: upper|lower|mid)     keltner (len, mult, atr_len, out)
  supertrend (atr_len, mult, out: line|dir)   cci mfi williams_r obv vwap psar
  highest lowest sum stdev roc (src, len)  shift change (src, n)
  add sub mul div min max pow neg abs sqrt log round
Add "tf_mult": 3 to any node to compute it on a 3x longer timeframe (no lookahead).

Conditions: {"gt": [a, b]} lt gte lte eq · {"cross_above": [a, b]} cross_below
  {"between": [x, lo, hi]} · {"rising": [x, n]} falling · {"all": [...]} {"any": [...]} {"not": c}
Exits: "stop": {"kind": "pct", "value": 4} or {"kind": "atr", "mult": 2, "len": 14}.
If an indicator is not listed, build it from the functions above.

My strategy: `;

export const PROMPT_PLACEHOLDER = 'describe your idea in plain English here';

export const EXAMPLE_SPEC = {
  name: 'macd_trend',
  title: 'MACD with a trend filter',
  description: 'Buy when MACD crosses up while price is above its 200 EMA; exit on the cross back down.',
  style: 'trend',
  indicators: {
    macd: { fn: 'macd', src: 'close', fast: '$fast', slow: '$slow', signal: 9, out: 'macd' },
    sig: { fn: 'macd', src: 'close', fast: '$fast', slow: '$slow', signal: 9, out: 'signal' },
    trend: { fn: 'ema', src: 'close', len: 200 },
  },
  entry_long: { all: [{ cross_above: ['macd', 'sig'] }, { gt: ['close', 'trend'] }] },
  exit_long: { cross_below: ['macd', 'sig'] },
  stop: { kind: 'atr', mult: '$atr_mult', len: 14 },
  defaults: { fast: 12, slow: 26, atr_mult: 2.5 },
  grid: { fast: [8, 12, 16], slow: [26, 40], atr_mult: [2, 3] },
  constraints: ['fast < slow'],
};
