'use client';

import { useEffect, useRef, useState } from 'react';

import type { Curve } from '../lib/types';

/* Strategy equity against buy-and-hold, with the training window shaded.
 *
 * Canvas, same rules as CandleChart: it paints on change (data, size, hover)
 * and then stops. The one animation -- the line drawing itself in when a
 * result first appears -- is a bounded 1.4s, not a loop.
 *
 * Log scale whenever the curve spans more than 10x. BTC buy-and-hold from
 * 2011 is a 100,000x line; on a linear axis every strategy worth comparing to
 * it is a flat line along the bottom.
 */

type Props = { curve: Curve; trainEnd: number | null; capital?: number; animateKey?: string };

const PAD = { r: 66, t: 8, b: 24 };
const DRAW_MS = 1400;

export default function EquityChart({ curve, trainEnd, capital = 10_000, animateKey }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<number | null>(null);
  const [k, setK] = useState(1);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => setSize({ w: Math.floor(e.contentRect.width), h: Math.floor(e.contentRect.height) }));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // Draw-in: runs once per new result, then stops.
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) { setK(1); return; }
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - t0) / DRAW_MS);
      setK(1 - (1 - t) ** 3);
      if (t < 1) raf = requestAnimationFrame(tick);
    };
    setK(0);
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [animateKey]);

  const n = curve.ts.length;
  const vals = [...curve.equity, ...curve.bh].filter((v) => v > 0);
  const lo = vals.length ? Math.min(...vals) : 1;
  const hi = vals.length ? Math.max(...vals) : 1;
  const log = hi / Math.max(lo, 1e-9) > 10;

  useEffect(() => {
    const cv = canvasRef.current;
    if (!cv || !size.w || !size.h || n < 2) return;
    const dpr = window.devicePixelRatio || 1;
    cv.width = size.w * dpr;
    cv.height = size.h * dpr;
    const g = cv.getContext('2d')!;
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, size.w, size.h);

    const W = size.w - PAD.r, H = size.h - PAD.t - PAD.b;
    const t0 = curve.ts[0], t1 = curve.ts[n - 1];
    const xt = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * W;
    const x = (i: number) => xt(curve.ts[i]);
    const f = (v: number) => (log ? Math.log(Math.max(v, lo)) : v);
    const span = f(hi) - f(lo) || 1;
    const ylo = f(lo) - span * 0.03, yhi = f(hi) + span * 0.03;
    const y = (v: number) => PAD.t + (1 - (f(v) - ylo) / (yhi - ylo)) * H;

    if (trainEnd && trainEnd > t0) {
      const tx = Math.min(xt(trainEnd), W);
      g.fillStyle = 'rgba(255,255,255,0.022)';
      g.fillRect(0, PAD.t, tx, H);
      g.strokeStyle = '#2a2f36';
      g.setLineDash([3, 4]);
      g.beginPath(); g.moveTo(Math.round(tx) + 0.5, PAD.t); g.lineTo(Math.round(tx) + 0.5, PAD.t + H); g.stroke();
      g.setLineDash([]);
      g.fillStyle = '#656c74';
      g.font = '10.5px system-ui, sans-serif';
      g.fillText('TRAIN', 10, PAD.t + 16);
      g.fillText('TEST', tx + 10, PAD.t + 16);
    }

    g.font = '10.5px ui-monospace, Menlo, Consolas, monospace';
    g.textBaseline = 'middle';
    for (const v of ticks(lo, hi, log)) {
      const yy = Math.round(y(v)) + 0.5;
      if (yy < PAD.t || yy > PAD.t + H) continue;
      g.strokeStyle = '#171b20';
      g.beginPath(); g.moveTo(0, yy); g.lineTo(W, yy); g.stroke();
      g.fillStyle = '#656c74';
      g.fillText(money(v), W + 10, yy);
    }
    g.strokeStyle = '#262b31';
    const yc = Math.round(y(capital)) + 0.5;
    g.beginPath(); g.moveTo(0, yc); g.lineTo(W, yc); g.stroke();

    g.textBaseline = 'alphabetic';
    const y0 = new Date(t0 * 1000).getUTCFullYear(), y1 = new Date(t1 * 1000).getUTCFullYear();
    const every = Math.max(1, Math.ceil((y1 - y0 + 1) / Math.max(2, Math.floor(W / 80))));
    for (let yr = y0 + 1; yr <= y1; yr += every) {
      g.fillStyle = '#4b535c';
      g.fillText(String(yr), xt(Date.UTC(yr, 0, 1) / 1000) - 13, size.h - 6);
    }

    const m = Math.max(2, Math.floor(n * k));
    const trace = (arr: number[]) => {
      g.beginPath();
      for (let i = 0; i < m; i++) { const px = x(i), py = y(Math.max(arr[i], lo)); i ? g.lineTo(px, py) : g.moveTo(px, py); }
    };
    trace(curve.bh);
    g.strokeStyle = '#4b535c'; g.lineWidth = 1.2; g.stroke();
    // soft fill under the strategy line
    trace(curve.equity);
    g.lineTo(x(m - 1), PAD.t + H); g.lineTo(x(0), PAD.t + H); g.closePath();
    const grad = g.createLinearGradient(0, PAD.t, 0, PAD.t + H);
    grad.addColorStop(0, 'rgba(231,233,236,0.08)'); grad.addColorStop(1, 'rgba(231,233,236,0)');
    g.fillStyle = grad; g.fill();
    trace(curve.equity);
    g.strokeStyle = '#e7e9ec'; g.lineWidth = 1.7; g.stroke();
    g.lineWidth = 1;

    if (k < 1) {
      const hx = x(m - 1), hy = y(Math.max(curve.equity[m - 1], lo));
      g.fillStyle = 'rgba(255,255,255,.14)'; g.beginPath(); g.arc(hx, hy, 9, 0, 7); g.fill();
      g.fillStyle = '#fff'; g.beginPath(); g.arc(hx, hy, 3, 0, 7); g.fill();
    }
    if (hover !== null && k >= 1) {
      const hx = Math.round(x(hover)) + 0.5;
      g.strokeStyle = '#39414a';
      g.beginPath(); g.moveTo(hx, PAD.t); g.lineTo(hx, PAD.t + H); g.stroke();
      for (const [arr, c] of [[curve.equity, '#e7e9ec'], [curve.bh, '#9aa1a9']] as const) {
        g.fillStyle = c; g.beginPath(); g.arc(hx, y(Math.max(arr[hover], lo)), 3, 0, 7); g.fill();
      }
    }
  }, [curve, size, hover, k, trainEnd, lo, hi, log, capital, n]);

  function onMove(e: React.MouseEvent) {
    if (n < 2 || !size.w) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const t = curve.ts[0] + ((e.clientX - rect.left) / (size.w - PAD.r)) * (curve.ts[n - 1] - curve.ts[0]);
    let best = 0;
    for (let i = 1; i < n; i++) if (Math.abs(curve.ts[i] - t) < Math.abs(curve.ts[best] - t)) best = i;
    setHover(best);
  }

  return (
    <div className="eq">
      <div className="eq-legend">
        <span><i style={{ background: '#e7e9ec' }} />Strategy</span>
        <span><i style={{ background: '#4b535c' }} />Buy &amp; hold</span>
        <span className="eq-dim">{log ? 'log scale · ' : ''}shaded = training window</span>
        <span className="eq-read mono">
          {hover !== null && (
            <>{new Date(curve.ts[hover] * 1000).toISOString().slice(0, 10)}{'   '}
              <b>{money(curve.equity[hover])}</b>{'  strategy   '}{money(curve.bh[hover])}{'  hold'}</>
          )}
        </span>
      </div>
      <div ref={wrapRef} className="eq-canvas" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <canvas ref={canvasRef} style={{ width: size.w, height: size.h }} />
      </div>
    </div>
  );
}

function ticks(lo: number, hi: number, log: boolean): number[] {
  if (log) {
    const out: number[] = [];
    for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++) {
      out.push(10 ** e);
      if (hi / lo < 1000) out.push(3 * 10 ** e);
    }
    return out;
  }
  const raw = (hi - lo) / 4 || 1;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? mag;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) out.push(v);
  return out;
}

export function money(v: number): string {
  if (!Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  if (a >= 1e9) return `$${(v / 1e9).toFixed(1)}B`;
  if (a >= 1e6) return `$${(v / 1e6).toFixed(1)}M`;
  if (a >= 1e4) return `$${(v / 1e3).toFixed(0)}k`;
  if (a >= 1e3) return `$${(v / 1e3).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}
