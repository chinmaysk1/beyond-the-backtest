/* Small helpers over a strategy spec that the UI needs without a round trip
 * to the engine. The engine remains the authority -- it re-validates every
 * job -- so these only have to agree with it, not replace it. */

import type { StrategyInfo } from './types';

const CONSTRAINT = /^\s*([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*(<=|>=|==|!=|<|>)\s*([A-Za-z_]\w*|-?\d+(?:\.\d+)?)\s*$/;

function holds(constraints: string[], p: Record<string, number>): boolean {
  for (const c of constraints) {
    const m = CONSTRAINT.exec(c);
    if (!m) continue;
    const v = (t: string) => (/^-?\d/.test(t) ? Number(t) : p[t]);
    const x = v(m[1]);
    const y = v(m[3]);
    const ok = { '<': x < y, '<=': x <= y, '>': x > y, '>=': x >= y, '==': x === y, '!=': x !== y }[m[2]];
    if (!ok) return false;
  }
  return true;
}

/** Grid combinations after constraints -- the same count `spec.grid()` runs. */
export function comboCount(
  grid: Record<string, number[]>,
  defaults: Record<string, number>,
  constraints: string[],
): number {
  const keys = Object.keys(grid).sort();
  let n = 0;
  const walk = (i: number, p: Record<string, number>) => {
    if (i === keys.length) { if (holds(constraints, p)) n += 1; return; }
    for (const v of grid[keys[i]]) walk(i + 1, { ...p, [keys[i]]: v });
  };
  walk(0, { ...defaults });
  return n;
}

/** What the picker shows for a spec, library or custom alike. */
export function strategyInfo(name: string, s: Record<string, any>): StrategyInfo {
  return {
    name,
    title: s.title ?? name,
    description: s.description ?? '',
    style: s.style ?? 'other',
    timeframes: s.timeframes ?? [],
    defaults: s.defaults ?? {},
    labels: s.labels ?? {},
    combos: comboCount(s.grid ?? {}, s.defaults ?? {}, s.constraints ?? []),
  };
}
