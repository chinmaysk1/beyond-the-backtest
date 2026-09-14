'use client';

import { useCallback, useEffect, useRef } from 'react';

import type { Bar } from '../lib/adjust';

/* The candlestick chart.
 *
 * One canvas, drawn on demand. There is deliberately NO requestAnimationFrame
 * loop: an early prototype ran a continuous render plus backdrop-filter and CSS
 * 3D transforms, and it dropped frames on every interaction. Everything here
 * paints once per change -- data, resize, crosshair, pan, zoom -- then stops.
 *
 * Interaction follows TradingView, because that is what anyone who reads charts
 * already has in their hands:
 *
 *   wheel                  zoom in / out, anchored under the cursor
 *   drag on the plot       pan through history
 *   drag the price axis    stretch or compress the vertical scale
 *   drag the time axis     zoom, same as the wheel
 *   double-click an axis   reset that axis
 *   crosshair              floating labels on BOTH axes, not just a line
 *
 * The window-less treatment: no frame, no background of its own. The series
 * spans the full width and the oldest candles fade toward the left, so it
 * recedes behind the floating panel instead of stopping at an edge. That depth
 * is a per-candle alpha ramp -- cheap, and it never touches the compositor.
 */

const COLORS = {
  up: '#2f9e79',
  upDim: 'rgba(47,158,121,0.55)',
  down: '#cf5a52',
  downDim: 'rgba(207,90,82,0.55)',
  grid: 'rgba(255,255,255,0.035)',
  axisFaint: '#4c5259',
  crosshair: 'rgba(255,255,255,0.22)',
  label: '#2c323a',
  labelText: '#e7e9ec',
};

const PAD_RIGHT = 62;
const PAD_BOTTOM = 26;
const PAD_TOP = 28;
const FADE_FROM = 0.22;
const MIN_ALPHA = 0.3;

const MIN_VISIBLE = 25;      // most zoomed in
const MAX_VISIBLE = 1200;    // most zoomed out
const DEFAULT_VISIBLE = 140;

type Props = {
  bars: Bar[];
  /* Identifies the SERIES (symbol + timeframe + adjustment). The view resets on
   * this, never on the bars array -- prepending older history changes the array
   * but must not throw away where the user is looking. */
  seriesKey: string;
  scale?: 'linear' | 'log';
  showVolume?: boolean;
  /* Called when the view approaches the oldest bar held, so the page can fetch
   * more history. Without it the chart simply stops in 2023 and looks like the
   * data ends there. */
  onNeedMore?: () => void;
  onHover: (bar: Bar | null) => void;
};

export default function CandleChart({
  bars, seriesKey, scale = 'linear', showVolume = true, onNeedMore, onHover,
}: Props) {
  const ref = useRef<HTMLCanvasElement>(null);
  const size = useRef({ w: 0, h: 0 });
  const hover = useRef<{ x: number; y: number } | null>(null);

  const view = useRef({
    offset: 0,          // bars from the right edge
    visible: DEFAULT_VISIBLE,
    yScale: 1,          // manual vertical stretch, from dragging the price axis
  });

  const drag = useRef<
    | { kind: 'pan'; x: number; offset: number }
    | { kind: 'scaleY'; y: number; scale: number }
    | { kind: 'scaleX'; x: number; visible: number }
    | null
  >(null);

  const asked = useRef(0);
  const prevLen = useRef(0);
  const prevKey = useRef<string | null>(null);

  const geom = useCallback(() => {
    const { w, h } = size.current;
    const plotW = w - PAD_RIGHT;
    const volH = showVolume ? Math.max(40, h * 0.15) : 0;
    const plotH = h - PAD_TOP - PAD_BOTTOM - volH - (showVolume ? 10 : 0);
    return { w, h, plotW, plotH, volH };
  }, [showVolume]);

  const draw = useCallback(() => {
    const canvas = ref.current;
    const { w: W, h: H, plotW, plotH, volH } = geom();
    if (!canvas || !W || !bars.length) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    ctx.clearRect(0, 0, W, H);

    const v = view.current;
    const count = Math.max(2, Math.min(bars.length, Math.round(v.visible)));
    const slot = plotW / count;
    const end = Math.max(count, bars.length - v.offset);
    const slice = bars.slice(Math.max(0, end - count), end);
    if (!slice.length) return;

    let hi = -Infinity;
    let lo = Infinity;
    let maxV = 0;
    for (const b of slice) {
      if (b.high > hi) hi = b.high;
      if (b.low < lo) lo = b.low;
      if (b.volume > maxV) maxV = b.volume;
    }
    /* Log scale plots equal PERCENTAGE moves as equal distances, which is the
     * right view for anything that has changed by orders of magnitude -- BTC
     * from $2 to $77,000 is unreadable on a linear axis, because fourteen years
     * of history collapse onto the bottom pixel.
     *
     * Decided from the RAW low, BEFORE padding. Padding in linear space can
     * drive the low below zero on a wide range, and a non-positive low disables
     * log -- which is why zooming out far enough used to make the axis quietly
     * revert to linear partway through the history. */
    const useLog = scale === 'log' && lo > 0;
    const f = useLog ? Math.log : (n: number) => n;
    const fInv = useLog ? Math.exp : (n: number) => n;

    /* Pad, and apply the manual stretch from dragging the price axis, in MAPPED
     * space. Doing it in price space would make the padding a fixed number of
     * dollars, which is both wrong on a log axis and the source of the negative
     * low above. In mapped space it is a fixed fraction of the visible range at
     * every zoom level, and exp() can never produce a negative price. */
    const fMid = (f(hi) + f(lo)) / 2;
    const fHalf = ((f(hi) - f(lo)) / 2 || (useLog ? 0.05 : 1)) * 1.08 / v.yScale;
    const fHi = fMid + fHalf;
    const fLo = fMid - fHalf;
    hi = fInv(fHi);
    lo = fInv(fLo);

    const x = (i: number) => plotW - (count - i) * slot + slot / 2;
    const y = (p: number) =>
      PAD_TOP + ((fHi - f(Math.max(p, 1e-12))) / (fHi - fLo)) * plotH;
    const priceAt = (py: number) =>
      fInv(fHi - ((py - PAD_TOP) / plotH) * (fHi - fLo));
    const depth = (px: number) => {
      const start = W * FADE_FROM;
      if (px >= start) return 1;
      return MIN_ALPHA + (1 - MIN_ALPHA) * Math.max(0, px / start) ** 1.1;
    };

    // --- gridlines + price axis ---------------------------------------------
    ctx.font = '11px ui-monospace, "SF Mono", Menlo, monospace';
    ctx.textBaseline = 'middle';
    const ticks = Math.max(4, Math.min(10, Math.round(plotH / 46)));
    for (let i = 0; i <= ticks; i++) {
      const p = fInv(fLo + (fHi - fLo) * (i / ticks));
      const py = Math.round(y(p)) + 0.5;
      ctx.strokeStyle = COLORS.grid;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, py);
      ctx.lineTo(plotW, py);
      ctx.stroke();
      ctx.fillStyle = COLORS.axisFaint;
      ctx.textAlign = 'left';
      ctx.fillText(fmtPrice(p), plotW + 9, py);
    }

    // --- candles + volume ----------------------------------------------------
    const bodyW = Math.max(1, Math.min(slot * 0.68, 18));
    for (let i = 0; i < slice.length; i++) {
      const b = slice[i];
      const cx = x(i);
      const a = depth(cx);
      if (a <= 0.005) continue;

      const up = b.close >= b.open;
      ctx.globalAlpha = a;
      ctx.strokeStyle = up ? COLORS.up : COLORS.down;
      ctx.fillStyle = up ? COLORS.up : COLORS.down;

      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(Math.round(cx) + 0.5, y(b.high));
      ctx.lineTo(Math.round(cx) + 0.5, y(b.low));
      ctx.stroke();

      const yo = y(b.open);
      const yc = y(b.close);
      const bh = Math.max(1, Math.abs(yc - yo));
      const bx = cx - bodyW / 2;
      const by = Math.min(yo, yc);

      // The forming bar is hollow: its close is only the current price, and it
      // must not read as a settled candle.
      if (b.partial) {
        ctx.lineWidth = 1.2;
        ctx.strokeRect(Math.round(bx) + 0.5, Math.round(by) + 0.5,
          Math.max(1, Math.round(bodyW)), Math.round(bh));
      } else {
        ctx.fillRect(Math.round(bx), Math.round(by),
          Math.max(1, Math.round(bodyW)), Math.round(bh));
      }

      if (showVolume && maxV > 0) {
        const vh = (b.volume / maxV) * volH;
        ctx.globalAlpha = a * 0.38;
        ctx.fillRect(Math.round(bx), H - PAD_BOTTOM - vh,
          Math.max(1, Math.round(bodyW)), Math.round(vh));
      }
    }
    ctx.globalAlpha = 1;

    // --- last price ----------------------------------------------------------
    // The newest bar of the SERIES, not of the visible window, so panning into
    // history does not relabel an old level as "current price".
    const last = bars[bars.length - 1];
    if (last && last.close <= hi && last.close >= lo) {
      const ly = Math.round(y(last.close)) + 0.5;
      const up = last.close >= last.open;
      ctx.strokeStyle = up ? COLORS.upDim : COLORS.downDim;
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, ly);
      ctx.lineTo(plotW, ly);
      ctx.stroke();
      ctx.setLineDash([]);
      axisLabel(ctx, plotW, ly, fmtPrice(last.close), up ? COLORS.up : COLORS.down, '#0e1013');
    }

    // --- time axis -----------------------------------------------------------
    ctx.font = '11px var(--font-display), system-ui, sans-serif';
    ctx.fillStyle = COLORS.axisFaint;
    ctx.textAlign = 'center';
    const every = Math.max(1, Math.round(count / Math.max(3, plotW / 110)));
    for (let i = count - 1; i >= 0; i -= every) {
      const cx = x(i);
      if (cx < 4 || depth(cx) < 0.25) continue;
      ctx.fillText(fmtTime(slice[i].ts, count), cx, H - PAD_BOTTOM + 13);
    }

    // --- crosshair, with a label on EACH axis --------------------------------
    const hv = hover.current;
    if (hv && hv.x < plotW && hv.y < H - PAD_BOTTOM) {
      const hx = Math.round(hv.x) + 0.5;
      const hy = Math.round(hv.y) + 0.5;
      ctx.strokeStyle = COLORS.crosshair;
      ctx.setLineDash([2, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(hx, PAD_TOP);
      ctx.lineTo(hx, H - PAD_BOTTOM);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(0, hy);
      ctx.lineTo(plotW, hy);
      ctx.stroke();
      ctx.setLineDash([]);

      // price under the cursor, on the price axis
      ctx.font = '11px ui-monospace, "SF Mono", Menlo, monospace';
      axisLabel(ctx, plotW, hy, fmtPrice(priceAt(hv.y)), COLORS.label, COLORS.labelText);

      // time under the cursor, on the time axis
      const idx = Math.max(0, Math.min(slice.length - 1, Math.round((hv.x - slot / 2) / slot)));
      const stamp = fmtStamp(slice[idx].ts, count);
      const tw = ctx.measureText(stamp).width + 14;
      ctx.fillStyle = COLORS.label;
      roundRect(ctx, hx - tw / 2, H - PAD_BOTTOM + 3, tw, 17, 3);
      ctx.fill();
      ctx.fillStyle = COLORS.labelText;
      ctx.textAlign = 'center';
      ctx.fillText(stamp, hx, H - PAD_BOTTOM + 12);
    }
  }, [bars, geom, showVolume, scale]);

  const barAt = useCallback((px: number): Bar | null => {
    const { plotW } = geom();
    if (!bars.length || plotW <= 0) return null;
    const v = view.current;
    const count = Math.max(2, Math.min(bars.length, Math.round(v.visible)));
    const slot = plotW / count;
    const end = Math.max(count, bars.length - v.offset);
    const slice = bars.slice(Math.max(0, end - count), end);
    const i = Math.round((px - slot / 2) / slot);
    return slice[Math.max(0, Math.min(slice.length - 1, i))] ?? null;
  }, [bars, geom]);

  /* Ask the page for older bars when the window nears the oldest one held. The
   * guard stops a burst of requests while a fetch is already in flight. */
  const maybeLoadMore = useCallback(() => {
    if (!onNeedMore) return;
    const v = view.current;
    const oldestShown = bars.length - v.offset - v.visible;
    if (oldestShown > 60) return;
    if (Date.now() - asked.current < 1500) return;
    asked.current = Date.now();
    onNeedMore();
  }, [bars.length, onNeedMore]);

  const clampOffset = useCallback(() => {
    const v = view.current;
    v.offset = Math.max(0, Math.min(Math.max(0, bars.length - v.visible), v.offset));
  }, [bars.length]);

  const zoomAt = useCallback((factor: number, anchorPx: number) => {
    const { plotW } = geom();
    const v = view.current;
    const next = Math.max(MIN_VISIBLE, Math.min(MAX_VISIBLE, v.visible * factor));
    if (Math.abs(next - v.visible) < 0.5) return;

    // Keep the bar under the cursor under the cursor. Zooming about the centre
    // instead makes the chart feel like it is sliding away from you.
    const frac = Math.max(0, Math.min(1, anchorPx / plotW));
    const barsRightOfAnchor = v.visible * (1 - frac);
    const newRight = next * (1 - frac);
    v.offset += Math.round(barsRightOfAnchor - newRight);
    v.visible = next;
    clampOffset();
    draw();
    maybeLoadMore();
  }, [clampOffset, draw, geom, maybeLoadMore]);

  /* One effect owns the view, and it deliberately does NOT depend on `draw`.
   *
   * `draw` is a useCallback over `bars`, so it gets a new identity every time
   * older history is prepended. Listing it as a dependency meant the "new
   * series" reset re-ran on every page load -- which is exactly the symptom:
   * scroll back far enough to trigger a fetch, and the chart snaps to the
   * newest bars, zoomed in. The seriesKey guard never got a chance to help.
   *
   * The ref keeps the latest draw callable without making it a trigger. */
  const drawRef = useRef(draw);
  drawRef.current = draw;

  useEffect(() => {
    if (prevKey.current !== seriesKey) {
      // A genuinely different series: open at the live edge.
      view.current = { offset: 0, visible: DEFAULT_VISIBLE, yScale: 1 };
      prevKey.current = seriesKey;
      prevLen.current = 0;
    } else if (prevLen.current > 0 && bars.length > prevLen.current) {
      // Same series, older bars arrived at the FRONT. Every index shifts right
      // by however many were added, and `offset` counts back from the newest
      // bar, so it has to grow by the same amount or the window jumps forward
      // in time.
      view.current.offset += bars.length - prevLen.current;
    }
    prevLen.current = bars.length;

    const v = view.current;
    v.offset = Math.max(0, Math.min(Math.max(0, bars.length - v.visible), v.offset));
    drawRef.current();
  }, [seriesKey, bars]);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const apply = () => {
      const r = canvas.getBoundingClientRect();
      if (!r.width || !r.height) return;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      size.current = { w: r.width, h: r.height };
      canvas.width = Math.round(r.width * dpr);
      canvas.height = Math.round(r.height * dpr);
      canvas.getContext('2d')?.setTransform(dpr, 0, 0, dpr, 0, 0);
      draw();
    };
    apply();
    const ro = new ResizeObserver(apply);
    ro.observe(canvas);
    return () => ro.disconnect();
  }, [draw]);

  // Registered by hand: React's onWheel is passive and cannot preventDefault,
  // so the page would scroll instead of the chart zooming.
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      // A trackpad reports a two-finger horizontal swipe as deltaX. Treating
      // every wheel event as zoom made sideways swipes zoom instead of scroll,
      // which is not how any chart behaves. Horizontal intent pans; vertical
      // zooms.
      if (Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        const { plotW } = geom();
        const v = view.current;
        const slot = plotW / Math.max(2, v.visible);
        // Swiping RIGHT moves the view back in time (left), which is the
        // direction the content travels under your fingers. `offset` counts
        // bars back from the newest, so it moves opposite to the raw delta.
        const step = e.deltaX / slot;
        const moved = step > 0 ? Math.max(1, Math.round(step)) : Math.min(-1, Math.round(step));
        v.offset -= moved;
        clampOffset();
        draw();
        maybeLoadMore();
        return;
      }
      const r = canvas.getBoundingClientRect();
      const factor = e.deltaY > 0 ? 1.12 : 1 / 1.12;
      zoomAt(factor, e.clientX - r.left);
    };
    canvas.addEventListener('wheel', onWheel, { passive: false });
    return () => canvas.removeEventListener('wheel', onWheel);
  }, [zoomAt, geom, clampOffset, draw, maybeLoadMore]);

  /* Which region the pointer is in decides what a drag does. */
  const zoneOf = (px: number, py: number) => {
    const { w, h } = size.current;
    if (px > w - PAD_RIGHT) return 'scaleY';
    if (py > h - PAD_BOTTOM) return 'scaleX';
    return 'pan';
  };

  return (
    <canvas
      ref={ref}
      id="chart"
      onPointerDown={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const px = e.clientX - r.left;
        const py = e.clientY - r.top;
        const zone = zoneOf(px, py);
        const v = view.current;
        drag.current =
          zone === 'scaleY' ? { kind: 'scaleY', y: e.clientY, scale: v.yScale }
          : zone === 'scaleX' ? { kind: 'scaleX', x: e.clientX, visible: v.visible }
          : { kind: 'pan', x: e.clientX, offset: v.offset };
        e.currentTarget.classList.add(zone === 'pan' ? 'dragging' : `scale-${zone === 'scaleY' ? 'y' : 'x'}`);
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const px = e.clientX - r.left;
        const py = e.clientY - r.top;
        const d = drag.current;
        const v = view.current;

        if (d?.kind === 'pan') {
          const { plotW } = geom();
          const slot = plotW / Math.max(2, v.visible);
          v.offset = d.offset + Math.round((e.clientX - d.x) / slot);
          clampOffset();
          draw();
          maybeLoadMore();
          return;
        }
        if (d?.kind === 'scaleY') {
          // Downward drag compresses, upward expands -- the direction people
          // expect from pulling an axis.
          v.yScale = Math.max(0.25, Math.min(6, d.scale * (1 + (e.clientY - d.y) / 260)));
          draw();
          return;
        }
        if (d?.kind === 'scaleX') {
          v.visible = Math.max(MIN_VISIBLE, Math.min(MAX_VISIBLE,
            d.visible * (1 - (e.clientX - d.x) / 320)));
          clampOffset();
          draw();
          maybeLoadMore();
          return;
        }

        hover.current = { x: px, y: py };
        draw();
        onHover(barAt(px));
      }}
      onPointerUp={(e) => {
        drag.current = null;
        e.currentTarget.classList.remove('dragging', 'scale-y', 'scale-x');
      }}
      onPointerCancel={(e) => {
        drag.current = null;
        e.currentTarget.classList.remove('dragging', 'scale-y', 'scale-x');
      }}
      onDoubleClick={(e) => {
        const r = e.currentTarget.getBoundingClientRect();
        const zone = zoneOf(e.clientX - r.left, e.clientY - r.top);
        if (zone === 'scaleY') view.current.yScale = 1;
        else if (zone === 'scaleX') view.current.visible = DEFAULT_VISIBLE;
        else { view.current.offset = 0; view.current.visible = DEFAULT_VISIBLE; }
        clampOffset();
        draw();
      }}
      onPointerLeave={() => {
        hover.current = null;
        draw();
        onHover(null);
      }}
    />
  );
}

/* A pill on the price axis, as TradingView draws for the last price and the
 * crosshair. */
function axisLabel(ctx: CanvasRenderingContext2D, plotW: number, py: number,
                   text: string, bg: string, fg: string) {
  const w = Math.min(ctx.measureText(text).width + 14, PAD_RIGHT - 6);
  ctx.fillStyle = bg;
  roundRect(ctx, plotW + 4, py - 9, w, 18, 3);
  ctx.fill();
  ctx.fillStyle = fg;
  ctx.textAlign = 'left';
  ctx.fillText(text, plotW + 11, py + 0.5);
}

function roundRect(ctx: CanvasRenderingContext2D, x: number, y: number,
                   w: number, h: number, r: number) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function fmtPrice(p: number) {
  if (p >= 1000) return p.toFixed(0);
  if (p >= 10) return p.toFixed(2);
  return p.toFixed(4);
}

/* Axis granularity follows the zoom: showing "14:00" across four years of daily
 * bars is noise, and showing only a month name when zoomed into one session is
 * useless. */
function fmtTime(ts: number, visible: number) {
  const d = new Date(ts * 1000);
  if (visible > 400) {
    return d.toLocaleDateString('en-GB', { month: 'short', year: '2-digit', timeZone: 'UTC' });
  }
  if (visible > 90 || d.getUTCHours() === 0) {
    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', timeZone: 'UTC' });
  }
  return `${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}

function fmtStamp(ts: number, visible: number) {
  const d = new Date(ts * 1000);
  const date = d.toLocaleDateString('en-GB', {
    day: 'numeric', month: 'short', year: '2-digit', timeZone: 'UTC',
  });
  if (visible > 90) return date;
  return `${date} ${String(d.getUTCHours()).padStart(2, '0')}:${String(d.getUTCMinutes()).padStart(2, '0')}`;
}
