'use client';

import { useEffect, useRef, useState } from 'react';

import type { Curve } from '../lib/types';
import { moveHandle, type Handles, type Movable } from '../lib/windows';

/* Strategy equity against buy-and-hold, with the training window shaded.
 *
 * Canvas, same rules as CandleChart: it paints on change (data, size, hover)
 * and then stops. The one animation -- the line drawing itself in when a
 * result first appears -- is a bounded 1.4s, not a loop.
 *
 * Log scale whenever the curve spans more than 10x. BTC buy-and-hold from
 * 2011 is a 100,000x line; on a linear axis every strategy worth comparing to
 * it is a flat line along the bottom.
 *
 * With `windows`, the axis spans the whole series instead of the curve, and
 * three handles move train start, the train/test boundary and test end. The
 * holdout is drawn, locked, past the last one. The chart only reports moves:
 * `onChange` on every step for instant feedback, `onCommit` on release or key,
 * which is when the caller asks the engine for real numbers. Handles are DOM
 * sliders over the canvas, so pointer, touch and keyboard all work.
 */

export type WindowHandles = {
  value: Handles;                        // train start, test start, test end
  movable: Movable;
  holdout: [number, number];
  step: number;                          // one bar, in seconds
  onChange: (h: Handles) => void;
  onCommit: (h: Handles) => void;
  settled: boolean;                      // the numbers on screen are for `value`
};

type Props = { curve: Curve; trainEnd: number | null; capital?: number; animateKey?: string; windows?: WindowHandles };

const PAD = { r: 66, t: 8, b: 24 };
const DRAW_MS = 1400;
const HANDLES = [
  { label: 'Train start', cls: 'start' },
  { label: 'Train / test boundary', cls: 'mid' },
  { label: 'Test end', cls: 'end' },
] as const;

export default function EquityChart({ curve, trainEnd, capital = 10_000, animateKey, windows }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });
  const [hover, setHover] = useState<number | null>(null);
  const [k, setK] = useState(1);
  const [drag, setDrag] = useState<0 | 1 | 2 | null>(null);
  // The newest value, for the release handler: a pointerup can arrive before
  // the render that carries the last move.
  const latest = useRef<Handles | null>(null);
  latest.current = windows?.value ?? null;

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
  // The time axis: the whole series when windows can move, else the curve.
  const d0 = windows ? windows.movable.start : curve.ts[0];
  const d1 = windows ? windows.holdout[1] : curve.ts[n - 1];
  const plotW = size.w - PAD.r;
  const xt = (t: number) => ((t - d0) / Math.max(d1 - d0, 1)) * plotW;
  const wv = windows?.value;

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
    const t0 = d0, t1 = d1;
    const xt = (t: number) => ((t - t0) / Math.max(t1 - t0, 1)) * W;
    const x = (i: number) => xt(curve.ts[i]);
    const f = (v: number) => (log ? Math.log(Math.max(v, lo)) : v);
    const span = f(hi) - f(lo) || 1;
    const ylo = f(lo) - span * 0.03, yhi = f(hi) + span * 0.03;
    const y = (v: number) => PAD.t + (1 - (f(v) - ylo) / (yhi - ylo)) * H;

    if (windows && wv) {
      // Train shaded as before; the holdout hatched and labelled locked.
      const [a, b] = [xt(wv[0]), xt(wv[1])];
      g.fillStyle = 'rgba(255,255,255,0.022)';
      g.fillRect(a, PAD.t, b - a, H);
      const hx = xt(windows.holdout[0]);
      g.save();
      g.beginPath(); g.rect(hx, PAD.t, W - hx, H); g.clip();
      g.strokeStyle = '#1b1f24';
      for (let i = hx - H; i < W; i += 7) { g.beginPath(); g.moveTo(i, PAD.t + H); g.lineTo(i + H, PAD.t); g.stroke(); }
      g.restore();
      g.strokeStyle = '#2a2f36';
      g.beginPath(); g.moveTo(Math.round(hx) + 0.5, PAD.t); g.lineTo(Math.round(hx) + 0.5, PAD.t + H); g.stroke();
      g.fillStyle = '#656c74';
      g.font = '10.5px system-ui, sans-serif';
      g.fillText('TRAIN', a + 12, PAD.t + 16);
      g.fillText('TEST', b + 12, PAD.t + 16);
      const tag = W - hx > g.measureText('HOLDOUT · LOCKED').width + 16 ? 'HOLDOUT · LOCKED' : 'HOLDOUT';
      if (W - hx > g.measureText(tag).width + 16) g.fillText(tag, hx + 10, PAD.t + 16);
    } else if (trainEnd && trainEnd > t0) {
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

    if (windows && wv) {
      // Outside train and test: drawn over, so a curve from wider windows
      // visibly stops counting where the handles now are.
      g.fillStyle = 'rgba(15,17,20,0.72)';
      g.fillRect(0, PAD.t - 2, xt(wv[0]), H + 4);
      g.fillRect(xt(wv[2]), PAD.t - 2, xt(windows.holdout[0]) - xt(wv[2]), H + 4);
    }

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
  }, [curve, size, hover, k, trainEnd, lo, hi, log, capital, n, d0, d1, windows, wv]);

  const timeAt = (clientX: number) => {
    const rect = wrapRef.current!.getBoundingClientRect();
    return d0 + ((clientX - rect.left) / Math.max(plotW, 1)) * (d1 - d0);
  };

  function onMove(e: React.MouseEvent) {
    if (n < 2 || !size.w || drag !== null) return;
    const t = timeAt(e.clientX);
    if (t < curve.ts[0] || t > curve.ts[n - 1]) { setHover(null); return; }
    let best = 0;
    for (let i = 1; i < n; i++) if (Math.abs(curve.ts[i] - t) < Math.abs(curve.ts[best] - t)) best = i;
    setHover(best);
  }

  /* -- handles ---------------------------------------------------------------- */

  function move(which: 0 | 1 | 2, t: number) {
    if (!windows || !latest.current) return null;
    const next = moveHandle(latest.current, which, t, windows.movable, windows.step);
    latest.current = next;
    windows.onChange(next);
    return next;
  }

  function onKey(which: 0 | 1 | 2, e: React.KeyboardEvent) {
    if (!windows || !wv) return;
    // Arrows move a hundredth of the series, snapped; shift moves one bar.
    const { movable: m, step } = windows;
    const big = Math.max(step, Math.round((m.end - m.start) / 100 / step) * step);
    const by = e.shiftKey ? step : big;
    const t = e.key === 'ArrowLeft' || e.key === 'ArrowDown' ? wv[which] - by
      : e.key === 'ArrowRight' || e.key === 'ArrowUp' ? wv[which] + by
        : e.key === 'Home' ? -Infinity : e.key === 'End' ? Infinity : null;
    if (t === null) return;
    e.preventDefault();
    const next = move(which, Number.isFinite(t) ? t : t < 0 ? m.start - 1e12 : m.end + 1e12);
    if (next) windows.onCommit(next);
  }

  // A preview from the drawn curve while a handle moves: equity at one
  // boundary over equity at the other. Not the engine's number -- positions
  // carried across a boundary, costs already paid -- so it is labelled as an
  // estimate and gone once the real run lands.
  const eqAt = (t: number) => {
    if (n < 2 || t < curve.ts[0] || t > curve.ts[n - 1]) return null;
    let i = 0;
    while (i + 1 < n && curve.ts[i + 1] <= t) i++;
    return curve.equity[i] > 0 ? curve.equity[i] : null;
  };
  const ret = (a: number, b: number) => {
    const x = eqAt(a), y = eqAt(b);
    return x && y ? pctOf(y / x - 1) : 'n/a';
  };
  const previewing = windows && wv && (drag !== null || !windows.settled);

  return (
    <div className="eq">
      <div className="eq-legend">
        <span><i style={{ background: '#e7e9ec' }} />Strategy</span>
        <span><i style={{ background: '#4b535c' }} />Buy &amp; hold</span>
        <span className="eq-dim">{log ? 'log scale · ' : ''}{windows ? 'drag the lines to move train and test' : 'shaded = training window'}</span>
        <span className="eq-read mono">
          {previewing ? (
            <span className="eq-est" title="Read off the curve on screen. Release to have the engine re-run both windows.">
              estimate from this curve{'   '}train <b>{ret(wv[0], wv[1])}</b>{'   '}test <b>{ret(wv[1], wv[2])}</b>
            </span>
          ) : hover !== null && (
            <>{new Date(curve.ts[hover] * 1000).toISOString().slice(0, 10)}{'   '}
              <b>{money(curve.equity[hover])}</b>{'  strategy   '}{money(curve.bh[hover])}{'  hold'}</>
          )}
        </span>
      </div>
      <div ref={wrapRef} className="eq-canvas" onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
        <canvas ref={canvasRef} style={{ width: size.w, height: size.h }} />
        {windows && wv && size.w > 0 && HANDLES.map((h, i) => {
          const which = i as 0 | 1 | 2;
          const release = () => {
            if (drag !== which) return;
            setDrag(null);
            if (latest.current) windows.onCommit(latest.current);
          };
          return (
            <div key={h.cls} className={`eq-h ${h.cls} ${drag === which ? 'on' : ''}`} style={{ left: xt(wv[which]) }}
                 role="slider" tabIndex={0} aria-label={h.label}
                 aria-valuemin={windows.movable.start} aria-valuemax={windows.movable.end} aria-valuenow={wv[which]}
                 aria-valuetext={isoDay(wv[which])}
                 onPointerDown={(e) => {
                   if (e.button !== 0) return;
                   e.preventDefault();
                   e.currentTarget.setPointerCapture(e.pointerId);
                   e.currentTarget.focus();
                   setDrag(which);
                   setHover(null);
                 }}
                 onPointerMove={(e) => { if (drag === which) move(which, timeAt(e.clientX)); }}
                 onPointerUp={release} onPointerCancel={release}
                 onKeyDown={(e) => onKey(which, e)}>
              <i />
              <small className="mono">{isoDay(wv[which])}</small>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function isoDay(t: number): string {
  return new Date(t * 1000).toISOString().slice(0, 10);
}

function pctOf(v: number): string {
  const p = v * 100;
  return `${p >= 0 ? '+' : '−'}${Math.abs(p).toLocaleString('en-US', { maximumFractionDigits: Math.abs(p) >= 100 ? 0 : 1 })}%`;
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
