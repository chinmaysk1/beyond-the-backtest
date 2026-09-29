'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useMemo, useRef, useState } from 'react';

import { pct } from '../lib/format';
import type { TestSummary, VerdictLabel } from '../lib/types';

import type { Opened } from './StrategiesView';
import TestView from './TestView';

/* The Results tab: every test you have run, newest first, each with its
 * verdict. A row opens the test's page (TestView) -- the same page a run from
 * Strategies lands on -- with every setting its sweep tried.
 *
 * Built from the Data tab's parts: the page title with a pill filter, one
 * glass panel, the market's ticker badge, monospaced numbers. */

type Filter = VerdictLabel | 'none' | 'all';
const TONE: Record<VerdictLabel, string> = { ROBUST: 'good', WEAK: 'warn', OVERFIT: 'bad' };
const SCORING_POLL_MS = 3000;

export default function ResultsView({ open, setOpen, onNewTest }: {
  open: Opened | null; setOpen: (t: Opened | null) => void; onNewTest: () => void;
}) {
  if (open) {
    return <TestView id={open.id} sweepId={open.sweepId} onBack={() => setOpen(null)} onNewTest={onNewTest} />;
  }
  return <TestList onOpen={setOpen} onNewTest={onNewTest} />;
}

function TestList({ onOpen, onNewTest }: { onOpen: (t: Opened) => void; onNewTest: () => void }) {
  const router = useRouter();
  const [tests, setTests] = useState<TestSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>('all');
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  // Refetched while any test is still being scored, so its verdict appears
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

  const counts = useMemo(() => {
    const c: Record<Filter, number> = { all: 0, ROBUST: 0, WEAK: 0, OVERFIT: 0, none: 0 };
    for (const t of tests ?? []) { c.all++; c[t.label ?? 'none']++; }
    return c;
  }, [tests]);
  const shown = (tests ?? []).filter((t) => filter === 'all' || (t.label ?? 'none') === filter);
  const filters: [Filter, string][] = [['all', 'All'], ['ROBUST', 'Robust'], ['WEAK', 'Weak'], ['OVERFIT', 'Overfit']];
  if (counts.none) filters.push(['none', 'Unverified']);

  return (
    <div className="rv">
      <div className="head">
        <div className="title"><h1>Results</h1></div>
        <div className="seg">
          {filters.map(([f, label]) => (
            <button key={f} type="button" className={filter === f ? 'on' : ''} onClick={() => setFilter(f)}>
              {label}<span className="rv-n">{counts[f]}</span>
            </button>
          ))}
        </div>
        <div className="head-right rv-sum">
          {tests && `${counts.all} test${counts.all === 1 ? '' : 's'} · ${counts.ROBUST} robust`}
        </div>
      </div>

      <div className="rv-body">
        <section className="panel rv-list">
          {error ? <div className="rv-state">{error}</div>
            : !tests ? <div className="rv-state">Loading your tests…</div>
              : !tests.length ? (
                <div className="rv-state">
                  <p>No tests yet. Each one you run lands here, with its verdict.</p>
                  <button type="button" className="rv-go" onClick={onNewTest}>Run a test</button>
                </div>
              ) : (
                <>
                  <div className="rv-row rv-th" aria-hidden="true">
                    <span /><span>Strategy</span><span>Equity</span>
                    <span className="r">Test return</span><span className="r">Buy &amp; hold</span><span>Verdict</span><span className="r">Run</span>
                  </div>
                  {shown.map((t) => (
                    <button key={t.id} type="button" className="rv-row" onClick={() => onOpen({ id: t.id, sweepId: t.sweepId })}>
                      <span className="rv-tick">{t.symbol.replace('/USD', '').slice(0, 4)}</span>
                      <span className="rv-who">
                        <b>{t.strategy.replace(/_/g, ' ')}</b>
                        <small>{t.symbol} · {t.timeframe}{t.combos ? ` · ${t.combos} settings` : ''}{t.custom ? ' · moved windows' : ''}</small>
                      </span>
                      <Spark values={t.spark} />
                      <b className={`mono r ${(t.testPct ?? 0) >= 0 ? 'good' : 'bad'}`}>{pct(t.testPct)}</b>
                      <span className="mono r dim">{pct(t.bhPct)}</span>
                      <span>
                        {t.label ? <span className={`sv-chip ${TONE[t.label]}`}>{t.label.toLowerCase()}</span>
                          : <span className="sv-chip none">{t.scoring ? 'scoring…' : 'unverified'}</span>}
                      </span>
                      <span className="mono r dim">{ago(t.createdAt)}</span>
                    </button>
                  ))}
                  {!shown.length && <div className="rv-state">No tests with that verdict.</div>}
                </>
              )}
        </section>
      </div>
    </div>
  );
}

function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return <span />;
  const lo = Math.min(...values), hi = Math.max(...values);
  const y = (v: number) => (hi > lo ? 22 - ((v - lo) / (hi - lo)) * 20 : 12);
  const d = values.map((v, i) => `${i ? 'L' : 'M'}${((i / (values.length - 1)) * 100).toFixed(1)} ${y(v).toFixed(1)}`).join(' ');
  const up = values[values.length - 1] >= values[0];
  return (
    <svg className={`rv-spark ${up ? 'up' : 'down'}`} viewBox="0 0 100 24" preserveAspectRatio="none" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

function ago(iso: string): string {
  const d = new Date(iso);
  const days = (Date.now() - d.getTime()) / 86_400_000;
  if (days < 1) return d.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
}
