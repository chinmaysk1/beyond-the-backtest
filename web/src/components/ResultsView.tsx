'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import { pct } from '../lib/format';
import type { TestSummary } from '../lib/types';

import ScoreRing from './ScoreRing';
import type { Opened } from './StrategiesView';
import TestView from './TestView';

/* The Results tab: every test you have run, as a card each -- the strategy,
 * its market and timeframe, the score, the test-window return against simply
 * holding, and the settings it ran with. A filters panel on the left narrows
 * and sorts them. A card opens the test's page (TestView), the same page a
 * run from Strategies lands on.
 *
 * Nothing here blurs: the backdrop behind is static and already soft, and blur
 * inside a scrolling area is redrawn on every frame of the scroll. While the
 * list scrolls, cards ignore the pointer, so a still cursor does not set off
 * hover repaints on every card passing under it. See web/PLAN.md, "Glass". */

type Sort = 'score' | 'ret' | 'edge' | 'new';
const SORTS: [Sort, string][] = [['score', 'Score'], ['ret', 'Return'], ['edge', 'vs holding'], ['new', 'Newest']];
const TFS = ['5m', '15m', '30m', '1h', '4h', '1d'];
const SCORING_POLL_MS = 3000;

export default function ResultsView({ open, setOpen, onNewTest }: {
  open: Opened | null; setOpen: (t: Opened | null) => void; onNewTest: () => void;
}) {
  if (open) {
    return <TestView id={open.id} sweepId={open.sweepId} onBack={() => setOpen(null)} onNewTest={onNewTest} />;
  }
  return <TestGrid onOpen={setOpen} onNewTest={onNewTest} />;
}

type Filters = { q: string; sort: Sort; min: number; beat: boolean; markets: string[]; tfs: string[] };
const NO_FILTERS: Filters = { q: '', sort: 'score', min: 0, beat: false, markets: [], tfs: [] };

function TestGrid({ onOpen, onNewTest }: { onOpen: (t: Opened) => void; onNewTest: () => void }) {
  const router = useRouter();
  const [tests, setTests] = useState<TestSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [f, setF] = useState<Filters>(NO_FILTERS);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const body = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = body.current;
    if (!el) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    const on = () => {
      if (!t) el.classList.add('scrolling'); else clearTimeout(t);
      t = setTimeout(() => { el.classList.remove('scrolling'); t = null; }, 160);
    };
    el.addEventListener('scroll', on, { passive: true });
    return () => { el.removeEventListener('scroll', on); if (t) clearTimeout(t); };
  }, []);

  // Refetched while any test is still being scored, so its score appears
  // without a reload.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const load = () => fetch('/api/backtests').then(async (res) => {
      if (res.status === 401) { router.replace('/login'); return; }
      if (!res.ok) throw new Error('Could not load your tests');
      const out = (await res.json()).tests as TestSummary[];
      if (!alive.current) return;
      setTests(out);
      if (out.some((t) => t.scoring)) timer = setTimeout(load, SCORING_POLL_MS);
    }).catch((e: Error) => { if (alive.current) setError(e.message); });
    load();
    return () => { if (timer) clearTimeout(timer); };
  }, [router]);

  const markets = useMemo(() => [...new Set((tests ?? []).map((t) => t.symbol))], [tests]);
  const tfs = useMemo(() => TFS.filter((x) => (tests ?? []).some((t) => t.timeframe === x)), [tests]);
  const beat = (t: TestSummary) => t.testPct != null && t.bhPct != null && t.testPct > t.bhPct;

  const shown = useMemo(() => {
    const q = f.q.trim().toLowerCase();
    const xs = (tests ?? []).filter((t) =>
      (!q || `${t.strategy} ${t.symbol}`.toLowerCase().includes(q))
      && (!f.markets.length || f.markets.includes(t.symbol))
      && (!f.tfs.length || f.tfs.includes(t.timeframe))
      && (!f.min || (t.score ?? -1) >= f.min)
      && (!f.beat || beat(t)));
    // Unscored tests (still scoring, or failed) sort last under every order but Newest.
    const last = (v: number | null) => (v == null ? Infinity : -v);
    const by: Record<Sort, (t: TestSummary) => number> = {
      score: (t) => last(t.score),
      ret: (t) => last(t.testPct),
      edge: (t) => last(t.testPct == null || t.bhPct == null ? null : t.testPct - t.bhPct),
      new: (t) => -t.id,
    };
    return xs.sort((a, b) => by[f.sort](a) - by[f.sort](b) || b.id - a.id);
  }, [tests, f]);

  const toggle = (k: 'markets' | 'tfs', v: string) =>
    setF((x) => ({ ...x, [k]: x[k].includes(v) ? x[k].filter((y) => y !== v) : [...x[k], v] }));
  const n = tests?.length ?? 0;

  return (
    <div className="rv">
      <div className="head">
        <div className="title">
          <h1>Results</h1>
          {tests && <p>{n} test{n === 1 ? '' : 's'} · {tests.filter(beat).length} beat holding</p>}
        </div>
        <div className="head-right">
          {!!tests?.length && (
            <span className="rv-count">
              <span>{shown.length} of {n} tests</span>
              <span className="rv-legend"><i className="s" />Strategy <i className="h" />Holding</span>
            </span>
          )}
          <button type="button" className="glass-btn" onClick={onNewTest}>+ New test</button>
        </div>
      </div>

      <div className="rv-body" ref={body}>
        {error ? <div className="rv-state">{error}</div>
          : !tests ? <div className="rv-state">Loading your tests…</div>
            : !tests.length ? (
              <div className="rv-state">
                <p>No tests yet. Each one you run lands here, with its score.</p>
                <button type="button" className="glass-btn" onClick={onNewTest}>Run a test</button>
              </div>
            ) : (
              <div className="rv-layout">
                <aside className="glass rv-filters">
                  <div className="rv-ftop"><b>Filters</b>
                    <button type="button" className="rv-clear" onClick={() => setF((x) => ({ ...NO_FILTERS, sort: x.sort }))}>Clear</button></div>
                  <label className="rv-search">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                      <circle cx="11" cy="11" r="7" /><path d="M20 20l-3.5-3.5" /></svg>
                    <input value={f.q} placeholder="Search" aria-label="Search strategy or market"
                           onChange={(e) => setF((x) => ({ ...x, q: e.target.value }))} />
                  </label>
                  <div>
                    <div className="rv-fh">Sort by</div>
                    <div className="rv-sort">
                      {SORTS.map(([k, label]) => (
                        <button key={k} type="button" className={f.sort === k ? 'on' : ''}
                                onClick={() => setF((x) => ({ ...x, sort: k }))}>{label}</button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="rv-fh">Score at least</div>
                    <div className="rv-range">
                      <input type="range" min={0} max={100} value={f.min} aria-label="Minimum score"
                             onChange={(e) => setF((x) => ({ ...x, min: +e.target.value }))} />
                      <span className="mono">{f.min}</span>
                    </div>
                  </div>
                  <label className="rv-toggle">
                    <span>Beat buy-and-hold</span>
                    <input type="checkbox" checked={f.beat} onChange={(e) => setF((x) => ({ ...x, beat: e.target.checked }))} />
                    <i aria-hidden="true" />
                  </label>
                  <div>
                    <div className="rv-fh">Market</div>
                    <div className="rv-chips">
                      {markets.map((m) => (
                        <button key={m} type="button" className={f.markets.includes(m) ? 'on' : ''} onClick={() => toggle('markets', m)}>{m}</button>
                      ))}
                    </div>
                  </div>
                  <div>
                    <div className="rv-fh">Timeframe</div>
                    <div className="rv-chips">
                      {tfs.map((x) => (
                        <button key={x} type="button" className={f.tfs.includes(x) ? 'on' : ''} onClick={() => toggle('tfs', x)}>{x}</button>
                      ))}
                    </div>
                  </div>
                </aside>

                <section className="rv-grid">
                  {shown.map((t) => <Card key={t.id} t={t} onOpen={onOpen} />)}
                  {!shown.length && <div className="rv-state">No tests match these filters.</div>}
                </section>
              </div>
            )}
      </div>
    </div>
  );
}

function Card({ t, onOpen }: { t: TestSummary; onOpen: (t: Opened) => void }) {
  const settings = Object.entries(t.params).map(([k, v]) => setting(k, v, t.labels[k])).join(' · ');
  const meta = `${t.symbol} · ${t.timeframe}${t.custom ? ' · moved windows' : ''}`;
  return (
    <button type="button" className="glass rv-card" title={t.reason ?? undefined}
            onClick={() => onOpen({ id: t.id, sweepId: t.sweepId })}>
      <div className="rv-ctop">
        <div className="rv-cname"><b>{t.strategy.replace(/_/g, ' ')}</b><small>{meta}</small></div>
        <ScoreRing score={t.score} band={t.band}
                   title={t.scoring ? 'Scoring…' : t.score == null ? 'No score: the sweep failed, or it ran before scoring existed' : undefined} />
      </div>
      {t.score == null && t.scoring ? (
        <div className="rv-pnl"><span className="rv-vs">Scoring {t.combos ? `${t.combos} settings` : 'every setting'}…</span></div>
      ) : (
        <div>
          <div className="rv-pnl">
            <span className={`rv-big ${(t.testPct ?? 0) >= 0 ? 'good' : 'bad'}`}>{pct(t.testPct)}</span>
            <span className="rv-vs">vs {pct(t.bhPct)} holding</span>
          </div>
          <Spark equity={t.spark.equity} bh={t.spark.bh} up={(t.testPct ?? 0) >= 0} />
        </div>
      )}
      {settings && <div className="rv-params">{settings}</div>}
    </button>
  );
}

/* A setting as a short phrase: the spec's own name for it, shortened
 * ("Fast EMA length" -> "fast ema", "Stop loss %" -> "stop loss 4.1%"). */
function setting(k: string, v: number, label?: string): string {
  const name = (label ?? k.replace(/_/g, ' '))
    .replace(/ \(.*\)/, '').replace(/^Minimum trend strength/, 'min trend').replace(/ length$/i, '')
    .replace(/ %$/, '').toLowerCase();
  return `${name} ${v}${k.endsWith('_pct') ? '%' : ''}`;
}

/* Strategy and holding over the test window, on one scale. */
function Spark({ equity, bh, up }: { equity: number[]; bh: number[]; up: boolean }) {
  if (equity.length < 2) return <div className="rv-spark" />;
  const all = [...equity, ...bh].map((v) => Math.log(Math.max(v, 1e-6)));
  const lo = Math.min(...all), hi = Math.max(...all);
  const y = (v: number) => (hi > lo ? 38 - ((Math.log(Math.max(v, 1e-6)) - lo) / (hi - lo)) * 36 : 20);
  const d = (xs: number[]) => xs.map((v, i) => `${i ? 'L' : 'M'}${((i / (xs.length - 1)) * 100).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  return (
    <svg className="rv-spark" viewBox="0 0 100 40" preserveAspectRatio="none" aria-hidden="true">
      {bh.length > 1 && <path className="h" d={d(bh)} />}
      <path className={up ? 's up' : 's down'} d={d(equity)} />
    </svg>
  );
}
