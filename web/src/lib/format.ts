/* Numbers and dates, the same way everywhere they appear. */

export function pct(v: number | null | undefined, sign = true): string {
  if (v == null || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  const s = a.toLocaleString('en-US', { maximumFractionDigits: a >= 100 ? 0 : 1 });
  return `${sign ? (v >= 0 ? '+' : '−') : ''}${s}%`;
}

export function date(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

export function price(v: number): string {
  return v >= 1000 ? Math.round(v).toLocaleString('en-US') : v >= 10 ? v.toFixed(2) : v.toFixed(4);
}

export function clamp(v: number, a: number, b: number) { return Math.max(a, Math.min(b, v)); }
