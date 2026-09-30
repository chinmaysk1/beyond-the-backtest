'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { Bar } from '../lib/adjust';
import { flyCards, flyTitle } from '../lib/flight';
import { ROOMS, type RoomId } from '../lib/rooms';
import type { AdjustMode, Market } from '../lib/types';

import Brand from './Brand';
import CandleChart from './CandleChart';
import Landing, { type Stage } from './Landing';
import MarketList from './MarketList';
import SoonView from './SoonView';
import ResultsView from './ResultsView';
import StrategiesView, { loadLibrary, type Opened } from './StrategiesView';

/* Bars per request. Large enough that the first screen is never short, small
 * enough that switching symbols is instant on a 315,000-bar series. */
const PAGE = 1500;

const ADJUSTMENTS: { id: AdjustMode; label: string }[] = [
  { id: 'none', label: 'Raw' },
  { id: 'split', label: 'Split-adjusted' },
  { id: 'total', label: 'Total return' },
];

export default function Dashboard({ username }: { username: string }) {
  const router = useRouter();

  const [markets, setMarkets] = useState<Market[]>([]);
  const [symbol, setSymbol] = useState<string | null>(null);
  const [timeframe, setTimeframe] = useState('1d');
  const [adjust, setAdjust] = useState<AdjustMode>('split');
  const [bars, setBars] = useState<Bar[]>([]);
  const [more, setMore] = useState(false);   // an older-history fetch in flight
  const [scale, setScale] = useState<'linear' | 'log'>('linear');
  const [hovered, setHovered] = useState<Bar | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<RoomId>('data');
  // The test open in Results, if any: where a run from Strategies lands.
  const [opened, setOpened] = useState<Opened | null>(null);

  /* -- landing <-> app ------------------------------------------------------
   *
   * Every visit opens on the landing. Choosing a room flies the four cards
   * into the nav tabs and the wordmark into the brand, while the room rises
   * from below; the brand (or Esc) plays it backwards. See lib/flight.ts. */
  const [stage, setStage] = useState<Stage>('landing');
  const cardsRef = useRef<HTMLButtonElement[]>([]);
  const titleRef = useRef<HTMLHeadingElement>(null);
  const tabsRef = useRef<HTMLButtonElement[]>([]);
  const brandRef = useRef<HTMLElement>(null);

  const enter = useCallback(async (room: RoomId) => {
    if (stage !== 'landing') return;
    setTab(room);
    setStage('entering');
    const labels = ROOMS.map((r) => r.title);
    await Promise.all([
      flyCards(cardsRef.current, tabsRef.current, labels, ROOMS.findIndex((r) => r.id === room)),
      titleRef.current && brandRef.current ? flyTitle(titleRef.current, brandRef.current) : null,
    ]);
    setStage('app');
  }, [stage]);

  const home = useCallback(async () => {
    if (stage !== 'app') return;
    setStage('leaving');
    const labels = ROOMS.map((r) => r.title);
    await Promise.all([
      flyCards(cardsRef.current, tabsRef.current, labels, ROOMS.findIndex((r) => r.id === tab), true),
      titleRef.current && brandRef.current ? flyTitle(titleRef.current, brandRef.current, true) : null,
    ]);
    setStage('landing');
  }, [stage, tab]);

  // Warm the strategy library while the landing is up (see loadLibrary).
  useEffect(() => { loadLibrary().catch(() => {}); }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !(e.target as HTMLElement).matches('input, textarea, select')) home();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [home]);

  const active = useMemo(
    () => markets.find((m) => m.symbol === symbol) ?? null,
    [markets, symbol],
  );

  /* -- markets ------------------------------------------------------------ */

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch('/api/markets');
        if (res.status === 401) { router.replace('/login'); return; }
        const body = await res.json();
        if (cancelled) return;
        const list: Market[] = body.markets ?? [];
        setMarkets(list);
        if (list.length) {
          const first = list.find((m) => m.symbol === 'BTC/USD') ?? list[0];
          setSymbol(first.symbol);
          // Open on a timeframe this symbol actually has, rather than assuming
          // one and rendering an empty chart.
          const tfs = first.timeframes.map((t) => t.tf);
          setTimeframe(tfs.includes('1d') ? '1d' : tfs[tfs.length - 1]);
        }
      } catch {
        if (!cancelled) setError('Could not load markets');
      }
    })();
    return () => { cancelled = true; };
  }, [router]);

  /* -- bars --------------------------------------------------------------- */

  useEffect(() => {
    if (!symbol) return;
    let cancelled = false;
    setLoading(true);
    setError(null);

    (async () => {
      try {
        const q = new URLSearchParams({ symbol, timeframe, adjust, limit: String(PAGE) });
        const res = await fetch(`/api/bars?${q}`);
        if (res.status === 401) { router.replace('/login'); return; }
        const body = await res.json();
        if (cancelled) return;
        if (!res.ok) { setError(body.error ?? 'Could not load bars'); setBars([]); return; }
        setBars(body.bars ?? []);
        setHovered(null);
      } catch {
        if (!cancelled) setError('Could not load bars');
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [symbol, timeframe, adjust, router]);

  /* -- older history ------------------------------------------------------
   *
   * Panning left used to stop at whatever the first page held, which looked
   * exactly like the series ending there -- BTC appeared to have no data before
   * 2023 when the database holds it back to 2011. The chart asks for more as
   * the window nears the oldest bar loaded, and pages are prepended. */

  /* Kept in refs rather than read from state, so this callback has a stable
   * identity. Depending on `bars` would rebuild it on every page load, which
   * re-registers listeners in the chart for no reason. */
  const oldestRef = useRef<number | null>(null);
  const busyRef = useRef(false);
  const exhaustedRef = useRef(false);

  useEffect(() => {
    oldestRef.current = bars.length ? bars[0].ts : null;
  }, [bars]);

  // A different series has its own history; forget that the last one ran out.
  useEffect(() => {
    exhaustedRef.current = false;
    busyRef.current = false;
  }, [symbol, timeframe, adjust]);

  const loadMore = useCallback(async () => {
    const oldest = oldestRef.current;
    if (!symbol || oldest === null || busyRef.current || exhaustedRef.current) return;
    busyRef.current = true;
    setMore(true);
    try {
      const q = new URLSearchParams({
        symbol, timeframe, adjust, limit: String(PAGE), before: String(oldest),
      });
      const res = await fetch(`/api/bars?${q}`);
      if (!res.ok) return;
      const body = await res.json();
      const older: Bar[] = body.bars ?? [];
      if (!older.length) {
        // Genuinely the start of the series. Stop asking, or sitting at the
        // left edge fires a request every second and a half forever.
        exhaustedRef.current = true;
        return;
      }
      setBars((cur) => (cur.length && older[older.length - 1].ts >= cur[0].ts
        ? cur                                   // overlapping page, ignore
        : [...older, ...cur]));
    } catch {
      /* a failed page just means no more history this time */
    } finally {
      busyRef.current = false;
      setMore(false);
    }
  }, [symbol, timeframe, adjust]);

  /* -- selection ---------------------------------------------------------- */

  const pick = useCallback((m: Market) => {
    setSymbol(m.symbol);
    const tfs = m.timeframes.map((t) => t.tf);
    // Carry the current timeframe across only if the new symbol has it;
    // equities have no 4h, so switching from BTC would otherwise 404.
    if (!tfs.includes(timeframe)) setTimeframe(tfs.includes('1d') ? '1d' : tfs[tfs.length - 1]);
  }, [timeframe]);

  async function signOut() {
    await fetch('/api/auth/logout', { method: 'POST' });
    router.replace('/login');
    router.refresh();
  }

  const shown = hovered ?? bars[bars.length - 1] ?? null;

  return (
    <>
    <Landing stage={stage} onChoose={enter} cardsRef={cardsRef} titleRef={titleRef} />
    <div className={`app stage-${stage}`}>
      <nav className="nav">
        <Brand onClick={home} wordRef={brandRef} />

        <div className="tabs">
          {ROOMS.map((r, i) => (
            <button
              key={r.id}
              ref={(el) => { if (el) tabsRef.current[i] = el; }}
              className={tab === r.id ? 'on' : ''}
              type="button"
              // Results again, from inside a test, goes back to the list.
              onClick={() => { if (r.id === 'results' && tab === 'results') setOpened(null); setTab(r.id); }}
            >
              {r.title}{!r.live && <span className="soon-dot" />}
            </button>
          ))}
        </div>

        <div className="nav-right">
          <button
            className="icon-btn"
            type="button"
            onClick={signOut}
            title={`Sign out (${username})`}
            aria-label="Sign out"
          >
            <svg width="14" height="14" viewBox="0 0 16 16" fill="none"
                 stroke="currentColor" strokeWidth="1.5">
              <circle cx="8" cy="5.5" r="2.6" />
              <path d="M3 13.5c.8-2.4 2.6-3.6 5-3.6s4.2 1.2 5 3.6" />
            </svg>
          </button>
        </div>
      </nav>

      <div className="stage">
      {tab === 'strategies' ? <StrategiesView markets={markets} onDone={(t) => { setOpened(t); setTab('results'); }} />
        : tab === 'results' ? <ResultsView open={opened} setOpen={setOpened} onNewTest={() => setTab('strategies')} />
        : tab !== 'data' ? <SoonView room={tab} /> : (<>
      <div className="head">
        <div className="title">
          <h1>{symbol ?? 'â€”'}</h1>
        </div>

        <div className="seg">
          {(active?.timeframes ?? []).map((t) => (
            <button
              key={t.tf}
              type="button"
              className={t.tf === timeframe ? 'on' : ''}
              onClick={() => setTimeframe(t.tf)}
            >
              {t.tf}
            </button>
          ))}
        </div>

        <div className="head-right">
          <div className="seg">
            {ADJUSTMENTS.map((a) => (
              <button
                key={a.id}
                type="button"
                className={a.id === adjust ? 'on' : ''}
                onClick={() => setAdjust(a.id)}
              >
                {a.label}
              </button>
            ))}
          </div>
        </div>
      </div>

      <div className="work">
        <div className="canvas-wrap">
          <CandleChart
            bars={bars}
            seriesKey={`${symbol}|${timeframe}|${adjust}`}
            scale={scale}
            onHover={setHovered}
            onNeedMore={loadMore}
          />
        </div>

        <div className="overlay">
          <MarketList
            markets={markets}
            selected={symbol}
            onSelect={pick}
          />

          <div className="chart-col">
            <div className="ohlc mono">
              {error && <span style={{ color: 'var(--bad)' }}>{error}</span>}
              {!error && loading && <span>loadingâ€¦</span>}
              {!error && !loading && shown && <Ohlc bar={shown} />}
            </div>
          </div>
        </div>

        <button
          type="button"
          className={`scale-toggle ${scale === 'log' ? 'on' : ''}`}
          onClick={() => setScale(scale === 'log' ? 'linear' : 'log')}
          title={scale === 'log'
            ? 'Logarithmic scale — click for linear'
            : 'Linear scale — click for logarithmic'}
        >
          {scale === 'log' ? 'LOG' : 'LINEAR'}
        </button>
      </div>
      </>)}
      </div>
    </div>
    </>
  );
}

function Ohlc({ bar }: { bar: Bar }) {
  const col = bar.close >= bar.open ? 'var(--good)' : 'var(--bad)';
  const f = (v: number) => (v >= 1000 ? v.toFixed(0) : v >= 10 ? v.toFixed(2) : v.toFixed(4));
  return (
    <>
      <span>O<b style={{ color: col }}>{f(bar.open)}</b></span>
      <span>H<b style={{ color: col }}>{f(bar.high)}</b></span>
      <span>L<b style={{ color: col }}>{f(bar.low)}</b></span>
      <span>C<b style={{ color: col }}>{f(bar.close)}</b></span>
      {bar.partial && <span style={{ color: 'var(--warn)' }}>forming</span>}
    </>
  );
}
