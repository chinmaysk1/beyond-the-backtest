'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { date, pct, price } from '../lib/format';
import { pollJob, submitJob } from '../lib/jobs';
import type { Job, Metrics, SweepRow, Trade, Verdict } from '../lib/types';
import { TF_SECONDS, canMove, sameHandles, toSpans, type Handles, type WindowSpans } from '../lib/windows';

import EquityChart, { money } from './EquityChart';
import RankScatter from './RankScatter';
import ScoreRing, { BAND_TONE } from './ScoreRing';
import { loadLibrary } from './StrategiesView';

/* One test's page, inside the Results tab: where a run from Strategies lands,
 * and where a row of the Results list opens.
 *
 *   top       the return on data it never saw, and the score on it
 *   chart     the equity curve; its handles move train and test
 *   sweep     every setting the sweep tried, each with its own score
 *   judged    how the score was reached: walk-forward periods and the
 *             overfitting test's distribution
 *   trades
 *
 * Every number comes from the engine for the windows it ran on. Moving a
 * handle re-queues the backtest and its sweep for the new windows, and until
 * they land the old numbers are dimmed rather than passed off as current.
 * The score never moves with them: it is always scored on the defaults.
 *
 * Re-runs made here (moved windows, a chosen row) carry `of`, the test they
 * belong to, so they stay views of this test rather than new rows in Results.
 */

type Window = 'train' | 'test' | 'full' | 'holdout';

const COMMIT_MS = 650;        // a pause after moving a window before it is re-run

type Props = { id: number; sweepId: number | null; onBack: () => void; onNewTest: () => void };

export default function TestView({ id, sweepId, onBack, onNewTest }: Props) {
  const router = useRouter();
  const [bt, setBt] = useState<Job | null>(null);
  const [sw, setSw] = useState<Job | null>(null);
  const [meta, setMeta] = useState<{ title: string; labels: Record<string, string> }>({ title: '', labels: {} });
  const [view, setView] = useState<Window>('test');
  // The settings picked from the sweep, if any. Kept as params, not a rank:
  // moving the windows re-ranks, and the pick should survive that.
  const [chosenParams, setChosenParams] = useState<Record<string, number> | null>(null);
  const [rerunning, setRerunning] = useState(false);
  // Moved windows: the handles' position while it differs from what the
  // results were run on (null: follow the results), and the re-run under way.
  const [draft, setDraft] = useState<Handles | null>(null);
  const [rewind, setRewind] = useState<{ stage: 'backtest' | 'sweep'; progress: number } | null>(null);
  // Bringing the holdout up to the newest bar, on open (see the effect below).
  const [extending, setExtending] = useState(false);
  const extended = useRef<number | null>(null);
  const commitTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wanted = useRef<Record<string, unknown> | null>(null);   // the next re-run's request
  const busy = useRef(false);
  const winSeq = useRef(0);                                      // bumped when another test opens
  const [error, setError] = useState<string | null>(null);
  // Set on mount as well as cleared on unmount: React's development mode
  // mounts, unmounts and remounts every component once, and a flag that is only
  // ever cleared stays false after that -- silently dropping every update.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);
  const live = () => alive.current;

  const chosen = useMemo(() => {
    if (!chosenParams) return null;
    const row = sw?.result?.top?.find((r) => sameParams(r.params, chosenParams))
      ?? sw?.result?.rows?.find((r) => sameParams(r.params, chosenParams));
    return row?.test_rank ?? null;
  }, [chosenParams, sw]);

  /* -- load ---------------------------------------------------------------- */

  function failed(e: unknown) {
    const m = (e as Error).message;
    if (m === 'signed out') router.replace('/login');
    else if (m !== 'gone') setError(m);
  }

  useEffect(() => {
    winSeq.current++;
    setBt(null); setSw(null); setError(null); setChosenParams(null);
    setDraft(null); setRewind(null); setView('test');
    pollJob(id, setBt, live)
      .then((b) => { if (b.status !== 'done') setError(b.error ?? 'This backtest failed'); })
      .catch(failed);
    if (sweepId) pollJob(sweepId, setSw, live).catch(failed);
  }, [id, sweepId]); // eslint-disable-line react-hooks/exhaustive-deps

  // The holdout runs to the newest bar. A test run before holdouts existed has
  // none, and any test's falls behind as new bars close; either way, on open,
  // its holdout is rerun on the newest data and written back onto the test
  // (an `extend` backtest). Train, test and the score are untouched. Once per
  // open, and only while the page still shows the test itself.
  useEffect(() => {
    const r = bt?.result;
    if (!bt || bt.id !== id || bt.status !== 'done' || !r?.windows || extended.current === id) return;
    const behind = !r.runs?.all || (bt.latestTs != null && bt.latestTs > r.windows.holdout.end);
    if (!behind) return;
    extended.current = id;
    const seq = winSeq.current;
    setExtending(true);
    const w = r.windows;
    submitJob(body({
      kind: 'backtest', extend: id, ...(r.params ? { params: r.params } : {}),
      windows: { train: [w.train.start, w.train.end], test: [w.test.start, w.test.end] },
    }))
      .then(({ id: jid }) => pollJob(jid, () => {}, live))
      .then((j) => (j.status === 'done' ? pollJob(id, () => {}, live) : null))
      .then((fresh) => {
        // Only if nothing has replaced the test on screen meanwhile.
        if (fresh && seq === winSeq.current) setBt((cur) => (cur && cur.id === id ? fresh : cur));
      })
      .catch(() => {})            // a holdout that cannot be refreshed keeps the stored one
      .finally(() => { if (seq === winSeq.current) setExtending(false); });
  }, [bt]); // eslint-disable-line react-hooks/exhaustive-deps

  // Title and column labels come from the spec: the library's, or the user's
  // own stored one.
  const strategyKey = bt ? `${(bt.payload as any).strategy_id ?? ''}|${(bt.payload as any).strategy ?? ''}` : '';
  useEffect(() => {
    if (!bt) return;
    const p = bt.payload as { strategy?: string; strategy_id?: string };
    if (p.strategy_id) {
      fetch('/api/strategies/custom').then((r) => (r.ok ? r.json() : null)).then((out) => {
        const c = out?.strategies?.find((s: { id: string }) => s.id === p.strategy_id);
        if (c && alive.current) setMeta({ title: c.title ?? c.name, labels: c.spec?.labels ?? {} });
      }).catch(() => {});
    } else {
      loadLibrary().then((list) => {
        const s = list.find((x) => x.name === p.strategy);
        if (s && alive.current) setMeta({ title: s.title ?? s.name, labels: s.labels ?? {} });
      }).catch(() => {});
    }
  }, [strategyKey]); // eslint-disable-line react-hooks/exhaustive-deps

  /* -- re-runs ------------------------------------------------------------- */

  // A request for this test with something changed. Built from the test's
  // own payload, so it re-runs exactly the market, strategy and costs it had.
  function body(extra: Record<string, unknown>) {
    const p = bt!.payload as Record<string, unknown>;
    const b: Record<string, unknown> = { symbol: p.symbol, timeframe: p.timeframe, of: id, ...extra };
    if (p.costs) b.costs = p.costs;
    if (p.strategy_id) b.strategyId = p.strategy_id;
    else b.strategy = p.strategy;
    return b;
  }

  async function applyRow(row: SweepRow) {
    if (rerunning || rewind || !bt) return;
    const windows = spansOf(bt);
    const off = chosen === row.test_rank;          // clicking the chosen row goes back to the defaults
    setRerunning(true);
    setChosenParams(off ? null : row.params);
    try {
      const { id: rid } = await submitJob(body({
        kind: 'backtest', ...(off ? {} : { params: row.params }), ...(windows ? { windows } : {}),
      }));
      const job = await pollJob(rid, () => {}, live);
      if (job.status === 'done') setBt(job);
      else setError(job.error ?? 'The re-run failed');
    } catch (e) {
      failed(e);
    } finally {
      setRerunning(false);
    }
  }

  // Handles let go: after a short pause, re-run the backtest and its sweep on
  // the new windows. The request is built now, so a later re-run cannot pick
  // up a different pick. At most one re-run is in flight -- the queue allows a
  // few jobs per user and each carries a sweep -- so a move made meanwhile
  // waits and runs with the newest windows only.
  function commitWindows(h: Handles | null) {
    const r = bt?.result;
    if (!r?.default_windows) return;
    const def = handlesOf(r.default_windows);
    const next = h ?? def;
    setDraft(next);
    if (commitTimer.current) clearTimeout(commitTimer.current);
    if (!busy.current && sameHandles(next, handlesOf(r.windows))) { setDraft(null); return; }
    const windows = sameHandles(next, def) ? null : toSpans(next);
    const request = body({
      kind: 'backtest', withSweep: true,
      ...(chosenParams ? { params: chosenParams } : {}), ...(windows ? { windows } : {}),
    });
    commitTimer.current = setTimeout(() => {
      commitTimer.current = null;
      wanted.current = request;
      pump();
    }, h ? COMMIT_MS : 0);
  }

  async function pump() {
    if (busy.current || !wanted.current) return;
    const request = wanted.current;
    wanted.current = null;
    busy.current = true;
    const seq = winSeq.current;
    const current = () => seq === winSeq.current && !wanted.current && !commitTimer.current;
    setError(null);
    setRewind({ stage: 'backtest', progress: 0 });
    try {
      const ids = await submitJob(request);
      const b = await pollJob(ids.id, () => {}, live);
      if (b.status !== 'done') throw new Error(b.error ?? 'The re-run failed');
      if (current()) {
        setBt(b);
        setDraft(null);                  // the engine's snapped windows from here on
        setRewind({ stage: 'sweep', progress: 0 });
      }
      // The old ranking stays up, dimmed, until the new one is complete.
      const s = ids.sweepId ? await pollJob(ids.sweepId, (j) => {
        if (current()) setRewind({ stage: 'sweep', progress: j.progress ?? 0 });
      }, live) : null;
      if (current()) {
        if (s?.status === 'done') setSw(s);
        else if (s) setError(`The sweep failed: ${s.error}`);
        setDraft(null);
      }
    } catch (e) {
      if (seq === winSeq.current) failed(e);
    } finally {
      busy.current = false;
      if (wanted.current) pump();
      else if (seq === winSeq.current) setRewind(null);
    }
  }

  /* -- render -------------------------------------------------------------- */

  if (!bt?.result) {
    return (
      <div className="sv"><div className="sv-phase on"><div className="sv-results">
        <div className="sv-rtop"><BackButton onClick={onBack} /></div>
        <div className="rv-state">{error ?? (bt && bt.status !== 'done' ? 'This test is still running…' : 'Loading the test…')}</div>
      </div></div></div>
    );
  }
  return (
    <div className="sv"><div className="sv-phase on">
      <Results
        title={meta.title} bt={bt} sw={sw} view={view} setView={setView}
        chosen={chosen} picked={!!chosenParams} rerunning={rerunning} onUse={applyRow} error={error}
        draft={draft} setDraft={setDraft} onWindows={commitWindows} rewind={rewind} extending={extending}
        labels={meta.labels} onBack={onBack} onNewTest={onNewTest}
      />
    </div></div>
  );
}

function BackButton({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="sv-back" onClick={onClick}>
      <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M10 3L5 8l5 5" /></svg>
      All results
    </button>
  );
}

/* -- the page ------------------------------------------------------------------- */

type ResultsProps = {
  title: string; bt: Job; sw: Job | null; view: Window; setView: (w: Window) => void;
  chosen: number | null; picked: boolean; rerunning: boolean; onUse: (r: SweepRow) => void;
  onBack: () => void; onNewTest: () => void; error: string | null;
  labels: Record<string, string>;
  draft: Handles | null; setDraft: (h: Handles) => void; onWindows: (h: Handles | null) => void;
  rewind: { stage: 'backtest' | 'sweep'; progress: number } | null;
  extending: boolean;
};

function Results({ title, bt, sw, view, setView, chosen, picked, rerunning, onUse, onBack, onNewTest, error, labels,
                   draft, setDraft, onWindows, rewind, extending }: ResultsProps) {
  const r = bt.result!;
  // Older results have no holdout run; their Holdout tab falls back to test.
  const shownView: Window = r.metrics?.[view] ? view : 'test';
  const m = r.metrics![shownView]!;
  const w = r.windows;
  // The windows these numbers are for, and where the handles are now. They
  // differ from the moment a handle moves until the re-run lands.
  const ran = handlesOf(w);
  const shown = draft ?? ran;
  const settled = sameHandles(shown, ran) && rewind?.stage !== 'backtest';
  const movable = canMove(r.movable) ? r.movable : null;
  const def = r.default_windows ? handlesOf(r.default_windows) : null;
  const isDefault = !def || sameHandles(shown, def);
  const step = TF_SECONDS[r.timeframe] ?? 3600;
  const range = shownView === 'test' ? [w.test.start, w.test.end] : shownView === 'train' ? [w.train.start, w.train.end]
    : shownView === 'holdout' ? [w.holdout.start, w.holdout.end] : [w.train.start, w.test.end];
  const sr = sw?.status === 'done' ? sw.result : null;
  const rows = sr?.rows ?? sr?.top ?? [];
  // The score is for the settings on screen when they are among the scored
  // rows; otherwise the headline (#1 on the default windows) stands in. A
  // verdict stored before scores existed (and not yet rescored) counts as none.
  const verdict = typeof sr?.verdict?.score === 'number' ? sr.verdict : null;
  const ownRow = verdict && r.params
    ? Object.values(verdict.rows).find((x) => sameParams(x.params, r.params!)) ?? null : null;
  const shownVerdict = verdict ? ownRow ?? { score: verdict.score, band: verdict.band, reason: verdict.reason } : null;
  const varied = rows.length ? Object.keys(rows[0].params).filter((k) => rows.some((x) => x.params[k] !== rows[0].params[k])) : [];
  // Short column heads: the spec's label without its parenthetical.
  const labelOf = (k: string) => (labels[k] ?? k.replace(/_/g, ' ')).replace(/ \(.*\)/, '').replace(/^Minimum trend strength/, 'Min. trend');

  return (
    <div className="sv-results">
      <div className="sv-rtop">
        <BackButton onClick={onBack} />
        <div className="sv-crumb"><b>{title || r.strategy}</b> · {r.symbol} · {r.timeframe} · data through {date(r.as_of)}
          {r.adjust !== 'none' ? ` · ${r.adjust}-adjusted` : ''}</div>
        {verdict ? <ScoreRing score={shownVerdict!.score} band={shownVerdict!.band} size="sm" title={shownVerdict!.reason} />
          : <span className="sv-chip none" title={sw && sw.status !== 'done' && sw.status !== 'failed' ? 'Scored when the sweep finishes'
            : 'This test has no score: its sweep failed, or it ran before scoring existed'}>
            {sw && sw.status !== 'done' && sw.status !== 'failed' ? 'Scoring…' : 'Unscored'}</span>}
        <button type="button" className="sv-link" onClick={onNewTest}>New test</button>
        <Tabs value={shownView} onChange={setView} holdout={!!r.metrics?.holdout} />
      </div>

      <div className={`sv-hero ${rerunning || !settled ? 'dim' : ''}`}>
        <div>
          <div className="sv-hlabel">Return on {shownView === 'test' ? 'the test window' : shownView === 'train' ? 'the training window'
            : shownView === 'holdout' ? 'the holdout, after the test window' : 'train + test'} · {date(range[0])} – {date(range[1])}</div>
          <CountUp key={`${bt.id}-${shownView}`} className={`sv-big ${(m.net_pct ?? 0) >= 0 ? 'good' : 'bad'}`} value={m.net_pct ?? 0} />
          <div className="sv-vs">vs <span className="mono">{pct(m.bh_pct)}</span> buy &amp; hold · {chosen ? `settings #${chosen}` : picked ? 'your picked settings' : 'chosen settings'}</div>
          <p className="sv-say">{sentence(m, shownView)}</p>
        </div>
        <div className="sv-stats">
          <Stat label="Max drawdown" value={m.max_dd_pct == null ? '—' : `−${m.max_dd_pct.toFixed(1)}%`} tone="bad" />
          <Stat label="Sharpe" value={m.sharpe == null ? '—' : m.sharpe.toFixed(2)} />
          <Stat label="Trades" value={String(m.trades)} />
          <Stat label="Win rate" value={m.win_pct == null ? '—' : `${m.win_pct.toFixed(0)}%`} />
          <Stat label="Profit factor" value={m.profit_factor == null ? '—' : m.profit_factor.toFixed(2)} />
        </div>
      </div>
      {verdict && <ScoreRow v={verdict} row={shownVerdict!} own={ownRow} moved={!isDefault || (!!def && !sameHandles(ran, def))} />}
      {m.ruin && <div className="sv-warnline bad">This run lost the whole account. Everything after that point is flat at zero.</div>}
      {w.thin_sample && <div className="sv-warnline">
        {def && !sameHandles(ran, def) ? 'Train and test cover less than two years.' : 'Less than two years of data.'} Treat any result here as thin evidence.</div>}
      {error && <div className="sv-warnline bad">{error}</div>}

      {bt.curve && (
        <EquityChart
          curve={bt.curve} trainEnd={w.test.start} animateKey={String(bt.id)}
          windows={movable ? {
            value: shown, movable, holdout: [shown[2] + step, w.holdout.end], step, settled,
            onChange: setDraft, onCommit: onWindows,
          } : undefined}
        />
      )}
      {movable && (
        <div className="sv-wins">
          <span>Train <span className="mono">{date(shown[0])} – {date(settled ? w.train.end : shown[1] - step)}</span></span>
          <span>Test <span className="mono">{date(shown[1])} – {date(shown[2])}</span></span>
          <span>Holdout <span className="mono">{date(settled ? w.holdout.start : shown[2] + step)} – {date(w.holdout.end)}</span> · after test; never used to pick or score</span>
          <span className="sv-wins-state">
            {rewind?.stage === 'backtest' ? 'Re-running on these windows…'
              : rewind ? `Re-ranking every setting on the new test window… ${Math.round(rewind.progress * 100)}%`
                : !settled ? 'Release to re-run' : extending ? 'Bringing the holdout up to the latest bar…' : ''}
          </span>
          {!isDefault && <button type="button" className="sv-link" onClick={() => onWindows(null)}>Reset to default windows</button>}
        </div>
      )}

      <section className={`sv-section ${rewind ? 'sv-stale' : ''}`}>
        <div className="sv-sech"><h3>Every setting</h3>
          <p>{sr ? `All ${sr.combos} combinations, ranked on the test window (${date(sr.windows.test.start)} – ${date(sr.windows.test.end)}). Train rank alongside, because the two disagreeing is the warning sign.`
            : sw?.status === 'failed' ? `The sweep failed: ${sw.error}` : 'Still ranking…'}</p></div>
        {sr && (
          <div className="sv-best">
            <SweepTable rows={rows} combos={sr.combos ?? 0} verdict={verdict} varied={varied} labelOf={labelOf}
                        chosen={chosen} busy={rerunning || !!rewind} running={rerunning} onUse={onUse} />
            {sr.ranks && <RankScatter ranks={sr.ranks} combos={sr.combos ?? 0} chosen={chosen} corr={sr.rank_corr ?? null} />}
          </div>
        )}
      </section>
      {verdict && <Judged v={verdict} varied={varied} labelOf={labelOf} />}

      {bt.trades && <TradeList trades={bt.trades} testStart={w.test.start} holdoutStart={w.holdout.start} stale={!settled} />}

      <div className="sv-costnote">
        Every number above already pays <b>{m.costs?.commission_pct ?? 0}% commission + {m.costs?.slippage_bps ?? 0} bps slippage</b> on
        both sides of every trade: about {money((m.fees ?? 0) + (m.slippage ?? 0))} on a $10k start
        ({(m.cost_pct_of_capital ?? 0).toFixed(0)}% of it). Stops the price gaps through fill at the gap, not at the stop.
      </div>
    </div>
  );
}

function Tabs({ value, onChange, holdout }: { value: Window; onChange: (w: Window) => void; holdout: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [ul, setUl] = useState({ left: 0, width: 0 });
  useLayoutEffect(() => {
    const on = ref.current?.querySelector<HTMLButtonElement>('button.on');
    if (on) setUl({ left: on.offsetLeft, width: on.offsetWidth });
  }, [value]);
  return (
    <div className="sv-wtabs" ref={ref}>
      {(['train', 'test', 'full', ...(holdout ? ['holdout'] : [])] as Window[]).map((v) => (
        <button key={v} type="button" className={v === value ? 'on' : ''} onClick={() => onChange(v)}>
          {v === 'full' ? 'Train + test' : v[0].toUpperCase() + v.slice(1)}
        </button>
      ))}
      <span className="sv-ul" style={{ left: ul.left, width: ul.width }} />
    </div>
  );
}

/* -- verdict -------------------------------------------------------------------- */

const STATUS_TONE: Record<string, string> = { pass: 'good', warn: 'warn', fail: 'bad', na: '' };

// What each factor means, in a sentence or two, behind its info button.
const EXPLAIN = {
  dsr: 'The Sharpe ratio (return per unit of risk), marked down for how many settings were tried: trying many '
    + 'makes a lucky high score likely. This is the chance the edge is real after that markdown.',
  pbo: 'History is split in half thousands of different ways. Each time, we check whether the setting that won '
    + 'on one half stayed in the top half of the other. This is how often it did.',
  wf: 'Replays the choice as if it were happening live: pick the best settings using only the past, '
    + 'trade them on the next stretch, and repeat five times. This is the chance the next stretch makes money, '
    + 'judged from how many of the five did.',
};

const asPct = (x: number) => `${Math.round(x * 100)}%`;

/* The score and the three numbers it is the product of. A row's own deflated
 * Sharpe replaces the headline's; the other two judge the whole grid. */
function ScoreRow({ v, row, own, moved }: {
  v: Verdict; row: { score: number; band: Verdict['band']; reason: string }; own: { dsr: number | null } | null; moved: boolean;
}) {
  const f = { ...v.factors, ...(own ? { dsr: own.dsr ?? 0 } : {}) };
  const { walk_forward: wf, dsr } = v.checks;
  return (
    <div className="sv-score">
      <ScoreRing score={row.score} band={row.band} size="lg" />
      <div className="sv-stext">
        <p>{row.reason}</p>
        <Factor label={`Survives ${dsr.trials} setting${dsr.trials === 1 ? '' : 's'} tried`} tip={EXPLAIN.dsr} value={f.dsr} />
        <Factor label="Holds up on unseen data" tip={EXPLAIN.pbo} value={f.pbo} />
        <Factor label={wf.folds ? `Real-time picks made money, ${wf.folds_positive} of ${wf.folds}` : 'Real-time picks: not enough data'}
                tip={EXPLAIN.wf} value={f.walk_forward} />
        <div className="sv-snote mono">{row.score} = {asPct(f.dsr)} × {asPct(f.pbo)} × {asPct(f.walk_forward)}</div>
        {!own && <p className="sv-vnote">For the best setting; the one on screen was not among those scored.</p>}
        {moved && <p className="sv-vnote">Scored on the default windows, not the ones you moved. Moving windows can't change a score.</p>}
      </div>
    </div>
  );
}

function Factor({ label, tip, value }: { label: string; tip: string; value: number }) {
  return (
    <div className="sv-factor">
      <span>{label}
        <button type="button" className="sv-info" aria-label={`What does "${label.toLowerCase()}" mean?`}>i
          <span className="sv-tip" role="tooltip">{tip}</span></button></span>
      <span className="sv-track"><i style={{ width: asPct(Math.max(0, Math.min(1, value))) }} /></span>
      <b className="mono">{asPct(value)}</b>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: string; tone?: 'bad' }) {
  return <div className="sv-stat"><small>{label}</small><b className={tone ?? ''}>{value}</b></div>;
}

function CountUp({ value, className }: { value: number; className: string }) {
  const [v, setV] = useState(0);
  useEffect(() => {
    let raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / 1100);
      setV(value * (1 - (1 - k) ** 3));
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [value]);
  return <div className={className}>{pct(v)}</div>;
}

function TradeList({ trades, testStart, holdoutStart, stale }: { trades: Trade[]; testStart: number; holdoutStart: number; stale: boolean }) {
  const [all, setAll] = useState(false);
  const recent = [...trades].reverse();
  const shown = all ? recent : recent.slice(0, 14);
  return (
    <section className={`sv-section ${stale ? 'sv-stale' : ''}`}>
      <div className="sv-sech"><h3>Trades</h3><p>{trades.length} over train, test{trades.some((t) => t.entry_ts >= holdoutStart) ? ' and the holdout' : ''} · newest first</p></div>
      <div className="sv-tblwrap">
        <table className="sv-t">
          <thead><tr><th>Entry</th><th>Exit</th><th>Side</th><th>Entry</th><th>Exit</th><th>Why it closed</th><th>P&amp;L</th><th>Window</th></tr></thead>
          <tbody>
            {shown.map((t, i) => (
              <tr key={`${t.entry_ts}-${i}`}>
                <td className="mono">{date(t.entry_ts)}</td>
                <td className="mono">{date(t.exit_ts)}</td>
                <td><span className={`sv-side ${t.side}`}>{t.side}</span></td>
                <td className="mono">{price(t.entry_px)}</td>
                <td className="mono">{price(t.exit_px)}</td>
                <td className="dim">{t.reason === 'end' ? 'still open at the end' : t.reason === 'signal' ? 'exit signal' : t.reason.replace('_', ' ')}</td>
                <td className={`mono ${t.pnl_pct >= 0 ? 'good' : 'bad'}`}>{pct(t.pnl_pct)}</td>
                <td className="dim">{t.entry_ts >= holdoutStart ? 'holdout' : t.entry_ts >= testStart ? 'test' : 'train'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {trades.length > 14 && <button type="button" className="sv-link" style={{ marginTop: 12 }} onClick={() => setAll(!all)}>
        {all ? 'Show fewer' : `Show all ${trades.length}`}</button>}
    </section>
  );
}

/* -- windows -------------------------------------------------------------------- */

type SplitLike = { train: { start: number; end: number }; test: { start: number; end: number } };

/* The three handle positions a split corresponds to. */
function handlesOf(w: SplitLike): Handles {
  return [w.train.start, w.test.start, w.test.end];
}

/* The `windows` to send so a re-run matches this result: null when it ran on
 * the defaults (or predates moving windows), so it dedupes with them. */
function spansOf(job: Job | null): WindowSpans | null {
  const r = job?.result;
  if (!r?.default_windows || sameHandles(handlesOf(r.windows), handlesOf(r.default_windows))) return null;
  return { train: [r.windows.train.start, r.windows.train.end], test: [r.windows.test.start, r.windows.test.end] };
}

function sameParams(a: Record<string, number>, b: Record<string, number>): boolean {
  const ka = Object.keys(a);
  return ka.length === Object.keys(b).length && ka.every((k) => a[k] === b[k]);
}

/* -- words and numbers ---------------------------------------------------------- */

function sentence(m: Metrics, view: Window): string {
  const where = view === 'train' ? 'the training window' : view === 'test' ? 'data it was not trained on'
    : view === 'holdout' ? 'the holdout, after everything it was picked on' : 'train and test together';
  if (m.ruin) return `Lost the entire account on ${where}. Nothing else here matters.`;
  const net = m.net_pct ?? 0, bh = m.bh_pct ?? 0;
  if (net < 0) return `Lost ${pct(net, false)} on ${where}, while simply holding made ${pct(bh)}.`;
  if (net > bh) return `Beat buy-and-hold by ${Math.round(net - bh).toLocaleString()} points on ${where}, with a worst drop of ${(m.max_dd_pct ?? 0).toFixed(0)}% along the way.`;
  return `Made ${pct(net)} on ${where}, but simply holding made ${pct(bh)}.`;
}

/* -- the sweep ------------------------------------------------------------------ */

type Sort = 'test' | 'train' | 'score' | 'trades';
const PAGE = 12;

/* Every combination the sweep tried, not just the best: ranked on the test
 * window, each with its own score. Sortable, filterable by score. */
function SweepTable({ rows, combos, verdict, varied, labelOf, chosen, busy, running, onUse }: {
  rows: SweepRow[]; combos: number; verdict: Verdict | null; varied: string[]; labelOf: (k: string) => string;
  chosen: number | null; busy: boolean; running: boolean; onUse: (r: SweepRow) => void;
}) {
  const [sort, setSort] = useState<Sort>('test');
  const [min, setMin] = useState(0);
  const [all, setAll] = useState(false);
  const judged = useMemo(() => {
    const byKey = new Map(Object.values(verdict?.rows ?? {}).map((v) => [key(v.params), v]));
    return rows.map((r) => ({ r, v: byKey.get(key(r.params)) ?? null }));
  }, [rows, verdict]);
  const shown = useMemo(() => {
    const by: Record<Sort, (x: typeof judged[number]) => number> = {
      test: (x) => x.r.test_rank,
      train: (x) => x.r.train_rank,
      score: (x) => -(x.v?.score ?? -1),
      trades: (x) => -x.r.test.trades,
    };
    return judged.filter((x) => !min || (x.v?.score ?? 0) >= min).sort((a, b) => by[sort](a) - by[sort](b));
  }, [judged, min, sort]);
  const page = all ? shown : shown.slice(0, PAGE);
  const th = (s: Sort, label: string) => (
    <th><button type="button" className={`sv-sort ${sort === s ? 'on' : ''}`} onClick={() => setSort(s)}>{label}</button></th>
  );

  return (
    <div className="sv-sweep">
      {verdict && (
        <div className="sv-sweeptools">
          <div className="seg">
            {([['test', 'Test rank'], ['score', 'Score'], ['train', 'Train rank']] as const).map(([k, label]) => (
              <button key={k} type="button" className={sort === k ? 'on' : ''} onClick={() => setSort(k)}>{label}</button>
            ))}
          </div>
          <label className="sv-min">Score at least
            <input type="range" min={0} max={100} value={min} onChange={(e) => { setMin(+e.target.value); setAll(false); }} />
            <span className="mono">{min}</span></label>
        </div>
      )}
      <div className="sv-tblwrap">
        <table className="sv-t">
          <thead><tr>
            {th('test', '#')}{varied.map((k) => <th key={k}>{labelOf(k)}</th>)}
            <th>Test return</th>{th('train', 'Train rank')}{th('trades', 'Trades')}
            {verdict && <th>Deflated Sharpe</th>}{verdict && th('score', 'Score')}<th />
          </tr></thead>
          <tbody>
            {page.map(({ r, v }) => (
              <tr key={r.test_rank} className={chosen === r.test_rank ? 'sel' : ''}>
                <td className="mono">{r.test_rank}</td>
                {varied.map((k) => <td key={k} className="mono">{r.params[k]}</td>)}
                <td className={`mono ${(r.test.net_pct ?? 0) >= 0 ? 'good' : 'bad'}`}>{pct(r.test.net_pct)}{r.test.ruin ? ' · ruin' : ''}</td>
                <td className={`mono ${r.train_rank > r.test_rank + 8 ? 'warn' : ''}`}>{r.train_rank} <span className="dim">of {combos}</span></td>
                <td className="mono">{r.test.trades}</td>
                {verdict && <td className="mono">{v?.dsr == null ? '—' : v.dsr.toFixed(2)}</td>}
                {verdict && <td>{v ? <span className="sv-sbar" title={v.reason}><span><i className={BAND_TONE[v.band]} style={{ width: `${Math.max(v.score, 3)}%` }} /></span>
                  <b className="mono">{v.score}</b></span> : <span className="dim">—</span>}</td>}
                <td><button type="button" className="sv-use" disabled={busy} onClick={() => onUse(r)}>
                  {chosen === r.test_rank ? (running ? 'Running…' : 'Using') : 'Use'}</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!shown.length && <div className="sv-note" style={{ marginTop: 12 }}>No settings score that high.</div>}
      {shown.length > PAGE && <button type="button" className="sv-link" style={{ marginTop: 12 }} onClick={() => setAll(!all)}>
        {all ? 'Show fewer' : `Show all ${shown.length}`}</button>}
    </div>
  );
}

function key(p: Record<string, number>): string {
  return JSON.stringify(Object.keys(p).sort().map((k) => [k, p[k]]));
}

/* How the score was reached: the walk-forward periods, and the spread of
 * where the training winner landed out of sample across every split. */
function Judged({ v, varied, labelOf }: { v: Verdict; varied: string[]; labelOf: (k: string) => string }) {
  const wf = v.checks.walk_forward;
  const folds = wf.fold_pct ?? [];
  const top = Math.max(1, ...folds.map((x) => Math.abs(x)));
  const hist = v.pbo_logits ?? [];
  const hmax = Math.max(1, ...hist);
  const dsr = v.checks.dsr;
  return (
    <section className="sv-section">
      <div className="sv-sech"><h3>How the score was reached</h3>
        <p>All scored on the default windows ({date(v.windows.train.start)} – {date(v.windows.test.end)}), never anything after them.</p></div>
      <div className="sv-judge">
        <div className="sv-jcard">
          <h4>Walk-forward <span className={`mono ${STATUS_TONE[wf.status]}`}>{wf.oos_pct == null ? '—' : pct(wf.oos_pct)}</span></h4>
          <p>Each period trades the settings that were best on everything before it.
            {wf.bh_pct != null ? ` Holding over the same periods made ${pct(wf.bh_pct)}.` : ''}</p>
          {folds.length ? (
            <div className="sv-folds">
              {folds.map((f, i) => (
                <div key={i} className="sv-fold">
                  <div className="sv-fbar"><span className={f >= 0 ? 'up' : 'down'} style={{ height: `${(Math.abs(f) / top) * 50}%` }} /></div>
                  <b className={`mono ${f >= 0 ? 'good' : 'bad'}`}>{pct(f)}</b>
                  <small>Period {i + 1}</small>
                  {wf.pick_params?.[i] && <em title={Object.entries(wf.pick_params[i]).map(([k, x]) => `${labelOf(k)} ${x}`).join(' · ')}>
                    {varied.map((k) => wf.pick_params![i][k]).join(' / ') || 'same settings'}</em>}
                </div>
              ))}
            </div>
          ) : <div className="sv-note">Not enough data for five periods.</div>}
          {!!varied.length && <p className="sv-jfoot">Settings traded per period: {varied.map(labelOf).join(' / ')}. The pick changed {wf.pick_changed ?? 0} time{wf.pick_changed === 1 ? '' : 's'}.</p>}
        </div>
        <div className="sv-jcard">
          <h4>Overfitting test <span className={`mono ${STATUS_TONE[v.checks.pbo.status]}`}>
            {v.checks.pbo.value == null ? '—' : `${Math.round(v.checks.pbo.value * 100)}%`}</span></h4>
          <p>History split in half {v.checks.pbo.splits?.toLocaleString() ?? 'many'} ways. Each bar counts the splits where the
            training winner landed at that rank on the other half: left of the line is the bottom half.</p>
          {hist.length ? (
            <div className="sv-hist" role="img" aria-label="Distribution of the training winner's out-of-sample rank">
              {hist.map((n, i) => <span key={i} className={i < hist.length / 2 ? 'down' : 'up'} style={{ height: `${(n / hmax) * 100}%` }} />)}
              <i />
            </div>
          ) : <div className="sv-note">Not enough data, or nothing traded.</div>}
          <div className="sv-hlegend"><span>bottom half</span><span>top half</span></div>
          <p className="sv-jfoot">Deflated Sharpe {dsr.value == null ? '—' : dsr.value.toFixed(2)}: a daily Sharpe of {dsr.sharpe?.toFixed(3) ?? '—'} against
            {' '}{dsr.sr0?.toFixed(3) ?? '—'}, the best that {dsr.trials} settings would show by luck alone.</p>
        </div>
      </div>
    </section>
  );
}
