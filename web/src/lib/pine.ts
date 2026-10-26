/* What a Pine check returns (btb/pine/check.py CheckResult.as_dict, plus the
 * `name` and `spec` the check job adds for a script that passes). Shared by the
 * Strategies tab, POST /api/backtests and the MCP server, so all three read
 * one shape from one checker. */

export type PineMessage = { line: number; col: number; message: string; code?: string };

export type PineInput = {
  name: string | null;
  title: string;
  type: string;
  default: unknown;
  min: number | null;
  max: number | null;
  step: number | null;
};

export type PineCosts = {
  set: boolean;                       // the script chose its own costs
  commission_type: string | null;
  commission_value: number | null;
  slippage_ticks: number | null;
  zero: boolean;
};

export type PineCheck = {
  ok: boolean;
  sha: string;
  title: string;
  version: number | null;
  errors: PineMessage[];
  warnings: PineMessage[];
  inputs: PineInput[];
  grid: { values: Record<string, number[]>; constraints: string[]; combinations: number; raw: number };
  scored: boolean;
  costs: PineCosts;
  name?: string;
  spec?: Record<string, any>;
};

export const MAX_SOURCE_CHARS = 200_000;

export function messageText(m: PineMessage): string {
  return m.line ? `line ${m.line}${m.col ? `:${m.col}` : ''}: ${m.message}` : m.message;
}

export function describeCosts(c: PineCosts): string {
  if (!c.set) return 'Not set in the script: the market’s default costs apply';
  const commission = c.commission_type === 'percent' || c.commission_type == null
    ? `${c.commission_value ?? 0}% commission` : `${c.commission_value ?? 0} ${c.commission_type} commission`;
  return `From the script: ${commission} + ${c.slippage_ticks ?? 0} ticks slippage`;
}
