'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { AI_PROMPT, EXAMPLE_SPEC, PROMPT_PLACEHOLDER, checkSpecText, type SpecCheck } from '../lib/specCheck';
import type { Costs, Job, Market, Metrics, StrategyInfo, SweepRow, Trade } from '../lib/types';

import EquityChart, { money } from './EquityChart';
import RankScatter from './RankScatter';

/* The Strategies tab, as one flow in three phases:
 *
 *   compose   one question, centred: which market, which timeframe, which
 *             strategy (from the library, or your own JSON), at what cost
 *   running   the backtest and its sweep, live from the worker
 *   results   a page, not a grid of boxes: the return on data it never saw,
 *             the curve, the best settings and how much to trust them, trades
 *
 * "Run backtest" always queues the sweep too. Finding the best settings is not
 * a separate decision the user has to know to make -- and a best-settings list
 * without the train/test comparison beside it is exactly the misleading
 * number this product exists to replace.
 */

type Phase = 'compose' | 'running' | 'results';
type Window = 'train' | 'test' | 'full';

const ALL_TF = ['5m', '15m', '30m', '1h', '4h', '1d'];
// Mirrors btb/engine/fills.py DEFAULT_COSTS. The form starts here; the engine
// applies its own defaults if none are sent.
const DEFAULT_COSTS: Record<string, Costs> = {
  crypto: { commission_pct: 0.1, slippage_bps: 5 },
  equity: { commission_pct: 0, slippage_bps: 2 },
};
const POLL_MS = 600;
const MIN_RUN_MS = 2200;      // long enough to read what happened, never padded past that

export default function StrategiesView({ markets }: { markets: Market[] }) {
  const router = useRouter();
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [symbol, setSymbol] = useState<string | null>(null);
  const [timeframe, setTimeframe] = useState('4h');
  const [source, setSource] = useState<'lib' | 'json'>('lib');
  const [strategy, setStrategy] = useState('ema_cross_adx');
  const [jsonText, setJsonText] = useState('');
  const [costs, setCosts] = useState<Costs>(DEFAULT_COSTS.crypto);
  const [costsOpen, setCostsOpen] = useState(false);

  const [phase, setPhase] = useState<Phase>('compose');
  const [bt, setBt] = useState<Job | null>(null);
  const [sw, setSw] = useState<Job | null>(null);
  const [view, setView] = useState<Window>('test');
  const [chosen, setChosen] = useState<number | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set on mount as well as cleared on unmount: React's development mode
  // mounts, unmounts and remounts every component once, and a flag that is only
  // ever cleared stays false after that -- silently dropping every update.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  const market = useMemo(() => markets.find((m) => m.symbol === symbol) ?? null, [markets, symbol]);
  const spec = strategies.find((s) => s.name === strategy) ?? null;
  const check: SpecCheck | null = useMemo(() => (jsonText.trim() ? checkSpecText(jsonText) : null), [jsonText]);
  const combos = source === 'lib' ? spec?.combos ?? 0 : check?.ok ? check.combos : 0;
  const canRun = !!symbol && (source === 'lib' ? !!spec : !!check?.ok);

  /* -- load ---------------------------------------------------------------- */

  useEffect(() => {
    loadLibrary()
      .then((list) => { if (alive.current) setStrategies(list); })
      .catch((e: Error) => {
        if (e.message === 'signed out') router.replace('/login');
        else setError('Could not load the strategy library');
      });
  }, [router]);

  const pickMarket = useCallback((m: Market) => {
    setSymbol(m.symbol);
    setCosts(DEFAULT_COSTS[m.assetClass] ?? DEFAULT_COSTS.crypto);
    const have = m.timeframes.map((t) => t.tf);
    setTimeframe((cur) => (have.includes(cur) ? cur : have.includes('1d') ? '1d' : have[have.length - 1]));
  }, []);

  useEffect(() => {
    if (symbol || !markets.length) return;
    pickMarket(markets.find((m) => m.symbol === 'BTC/USD') ?? markets[0]);
  }, [markets, symbol, pickMarket]);

  /* -- jobs ---------------------------------------------------------------- */

  const poll = useCallback(async (id: number, onUpdate: (j: Job) => void): Promise<Job> => {
    for (;;) {
      if (!alive.current) throw new Error('gone');
      try {
        const res = await fetch(`/api/backtests/${id}`);
        if (res.status === 401) { router.replace('/login'); throw new Error('signed out'); }
        const job: Job = await res.json();
        onUpdate(job);
        if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') return job;
      } catch (e) {
        if ((e as Error).message === 'signed out') throw e;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }, [router]);

  function body(extra: Record<string, unknown>) {
    const b: Record<string, unknown> = { symbol, timeframe, costs, ...extra };
    if (source === 'json' && check?.ok) b.spec = check.spec;
    else b.strategy = strategy;
    return b;
  }

  async function submit(b: Record<string, unknown>) {
    const res = await fetch('/api/backtests', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b),
    });
    const out = await res.json();
    if (!res.ok) throw new Error(out.error ?? 'Could not queue the job');
    return out as { id: number; sweepId: number | null };
  }

  async function run() {
    if (!canRun) return;
    setError(null);
    setChosen(null);
    setView('test');
    setBt(null);
    setSw(null);
    const started = performance.now();
    let ids;
    try {
      ids = await submit(body({ kind: 'backtest', withSweep: true }));
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    setPhase('running');
    try {
      const [b] = await Promise.all([poll(ids.id, setBt), ids.sweepId ? poll(ids.sweepId, setSw) : null]);
      if (b.status !== 'done') {
        setError(b.error ?? 'The backtest failed');
        setPhase('compose');
        return;
      }
      const wait = MIN_RUN_MS - (performance.now() - started);
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      if (alive.current) setPhase('results');
    } catch {
      /* unmounted or signed out */
    }
  }

  async function applyRow(row: SweepRow) {
    if (rerunning) return;
    if (chosen === row.test_rank) {         // clicking the chosen row goes back to defaults
      setChosen(null);
      return run();
    }
    setRerunning(true);
    setChosen(row.test_rank);
    try {
      const { id } = await submit(body({ kind: 'backtest', params: row.params }));
      const job = await poll(id, () => {});
      if (job.status === 'done') setBt(job);
      else setError(job.error ?? 'The re-run failed');
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setRerunning(false);
    }
  }

  /* -- render -------------------------------------------------------------- */

  const title = source === 'json' && check?.ok ? (check.spec.title ?? check.spec.name) : spec?.title ?? '';

  return (
    <div className="sv">
      <div className={`sv-phase ${phase === 'compose' ? 'on' : ''}`}>
        <Compose
          markets={markets} market={market} pickMarket={pickMarket}
          timeframe={timeframe} setTimeframe={setTimeframe}
          source={source} setSource={setSource}
          strategies={strategies} strategy={strategy} setStrategy={setStrategy}
          jsonText={jsonText} setJsonText={setJsonText} check={check}
          costs={costs} setCosts={setCosts} costsOpen={costsOpen} setCostsOpen={setCostsOpen}
          combos={combos} canRun={canRun} onRun={run} error={error}
        />
      </div>

      <div className={`sv-phase ${phase === 'running' ? 'on' : ''}`}>
        {phase === 'running' && (
          <Running market={market} timeframe={timeframe} bt={bt} sw={sw} combos={combos} />
        )}
      </div>

      <div className={`sv-phase ${phase === 'results' ? 'on' : ''}`}>
        {phase === 'results' && bt?.result && (
          <Results
            title={title} bt={bt} sw={sw} view={view} setView={setView}
            chosen={chosen} rerunning={rerunning} onUse={applyRow} error={error}
            labels={source === 'json' && check?.ok ? check.spec.labels ?? {} : spec?.labels ?? {}}
            onBack={() => { setPhase('compose'); setError(null); }}
          />
        )}
      </div>
    </div>
  );
}

/* The library, fetched once per page load. Dashboard starts this while the
 * landing is still on screen, so the list is already there when the room
 * rises instead of flashing "Loading". A failure clears the cache so the next
 * visit to the tab retries. */
let libraryRequest: Promise<StrategyInfo[]> | null = null;
export function loadLibrary(): Promise<StrategyInfo[]> {
  libraryRequest ??= fetch('/api/strategies').then(async (res) => {
    if (res.status === 401) throw new Error('signed out');
    if (!res.ok) throw new Error('failed');
    return ((await res.json()).strategies ?? []) as StrategyInfo[];
  }).catch((e) => { libraryRequest = null; throw e; });
  return libraryRequest;
}

/* ============================================================ compose */

type ComposeProps = {
  markets: Market[]; market: Market | null; pickMarket: (m: Market) => void;
  timeframe: string; setTimeframe: (t: string) => void;
  source: 'lib' | 'json'; setSource: (s: 'lib' | 'json') => void;
  strategies: StrategyInfo[]; strategy: string; setStrategy: (s: string) => void;
  jsonText: string; setJsonText: (s: string) => void; check: SpecCheck | null;
  costs: Costs; setCosts: (c: Costs) => void; costsOpen: boolean; setCostsOpen: (b: boolean) => void;
  combos: number; canRun: boolean; onRun: () => void; error: string | null;
};

function Compose(p: ComposeProps) {
  const have = new Set(p.market?.timeframes.map((t) => t.tf) ?? []);
  const tfInfo = p.market?.timeframes.find((t) => t.tf === p.timeframe);
  const reason = (tf: string) => (p.market?.assetClass === 'equity' && tf === '4h'
    ? 'Yahoo does not serve 4h bars for stocks' : 'Not stored for this market yet');

  return (
    <div className="sv-compose">
      <div className="sv-composer">
        <div className="sv-kick">New backtest</div>
        <h2 className="sv-q">What do you want to test?</h2>
        <div className="sv-body">
          <div className="sv-field">
            <label>Market</label>
            <MarketPicker markets={p.markets} market={p.market} onPick={p.pickMarket} />
          </div>

          <div className="sv-field">
            <label>Timeframe</label>
            <div>
              <Seg
                value={p.timeframe}
                options={ALL_TF.map((tf) => ({ id: tf, label: tf, disabled: !have.has(tf), title: have.has(tf) ? undefined : reason(tf) }))}
                onChange={p.setTimeframe}
              />
              <div className="sv-note">
                {tfInfo
                  ? `${tfInfo.bars.toLocaleString()} bars since ${tfInfo.firstTs ? new Date(tfInfo.firstTs * 1000).getUTCFullYear() : '—'}`
                    + (tfInfo.bars < 2000 ? ' · thin: treat any result as weak evidence' : '')
                  : ''}
                {p.market?.assetClass === 'equity' ? ' · 4h isn’t available for stocks' : ''}
              </div>
            </div>
          </div>

          <div className="sv-field">
            <label>Strategy</label>
            <div>
              <Seg value={p.source} onChange={(v) => p.setSource(v as 'lib' | 'json')}
                   options={[{ id: 'lib', label: 'Library' }, { id: 'json', label: 'Custom JSON' }]} />
              {p.source === 'lib'
                ? <Library strategies={p.strategies} value={p.strategy} onChange={p.setStrategy} />
                : <CustomJson text={p.jsonText} setText={p.setJsonText} check={p.check} />}
            </div>
          </div>

          <div className="sv-field">
            <label>Costs</label>
            <div>
              <div className="sv-costs">
                <span><span className="mono">{p.costs.commission_pct.toFixed(2)}%</span> commission
                  + <span className="mono">{p.costs.slippage_bps} bps</span> slippage per trade</span>
                <button type="button" className="sv-link" onClick={() => p.setCostsOpen(!p.costsOpen)}>
                  {p.costsOpen ? 'done' : 'edit'}
                </button>
              </div>
              {p.costsOpen && (
                <div className="sv-costs-edit">
                  <label className="sv-num">
                    <input type="number" step="0.01" min="0" max="2" value={p.costs.commission_pct}
                           onChange={(e) => p.setCosts({ ...p.costs, commission_pct: clamp(Number(e.target.value) || 0, 0, 2) })} />
                    <span>% fee</span>
                  </label>
                  <label className="sv-num">
                    <input type="number" step="1" min="0" max="200" value={p.costs.slippage_bps}
                           onChange={(e) => p.setCosts({ ...p.costs, slippage_bps: clamp(Number(e.target.value) || 0, 0, 200) })} />
                    <span>bps slip</span>
                  </label>
                </div>
              )}
            </div>
          </div>

          <button type="button" className="sv-run" disabled={!p.canRun} onClick={p.onRun}>Run backtest</button>
          <div className="sv-run-sub">
            {p.canRun
              ? <>Runs the chosen settings, then tries all <b>{p.combos}</b> combinations and ranks them on data they never saw.</>
              : p.source === 'json' ? 'Paste a valid strategy to continue.' : 'Choose a market and a strategy.'}
          </div>
          {p.error && <div className="sv-error">{p.error}</div>}
        </div>
      </div>
    </div>
  );
}

function MarketPicker({ markets, market, onPick }: { markets: Market[]; market: Market | null; onPick: (m: Market) => void }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const ref = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', close);
    return () => document.removeEventListener('mousedown', close);
  }, []);
  useEffect(() => { if (open) setTimeout(() => input.current?.focus(), 40); }, [open]);
  const match = (m: Market) => `${m.symbol} ${m.name ?? ''}`.toLowerCase().includes(q.toLowerCase());
  const groups = [['crypto', 'Crypto'], ['equity', 'Equities']] as const;
  return (
    <div className={`sv-picker ${open ? 'open' : ''}`} ref={ref}>
      <button type="button" className="sv-pick" onClick={() => setOpen(!open)}>
        <span className="sv-tick">{(market?.symbol ?? '—').replace('/USD', '').slice(0, 4)}</span>
        <span><b>{market?.symbol ?? 'Choose a market'}</b><small>{market?.name}</small></span>
        <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M4 6l4 4 4-4" /></svg>
      </button>
      <div className="sv-pop">
        <input ref={input} value={q} onChange={(e) => setQ(e.target.value)} placeholder={`Search ${markets.length} markets`}
               onKeyDown={(e) => {
                 if (e.key === 'Enter') { const m = markets.find(match); if (m) { onPick(m); setOpen(false); setQ(''); } }
                 if (e.key === 'Escape') setOpen(false);
               }} />
        <div className="sv-pop-list">
          {groups.map(([k, label]) => {
            const rows = markets.filter((m) => m.assetClass === k && match(m));
            if (!rows.length) return null;
            return (
              <div key={k}>
                <h6>{label}</h6>
                {rows.map((m) => (
                  <button key={m.symbol} type="button" className={`sv-pop-item ${m.symbol === market?.symbol ? 'on' : ''}`}
                          onClick={() => { onPick(m); setOpen(false); setQ(''); }}>
                    <b>{m.symbol}</b><small>{m.name}</small>
                  </button>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

type SegOpt = { id: string; label: string; disabled?: boolean; title?: string };
function Seg({ value, options, onChange }: { value: string; options: SegOpt[]; onChange: (v: string) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [pill, setPill] = useState({ left: 0, width: 0 });
  useLayoutEffect(() => {
    const on = ref.current?.querySelector<HTMLButtonElement>('button.on');
    if (on) setPill({ left: on.offsetLeft, width: on.offsetWidth });
  }, [value, options.length]);
  return (
    <div className="sv-seg" ref={ref}>
      <span className="sv-seg-pill" style={{ left: pill.left, width: pill.width }} />
      {options.map((o) => (
        <button key={o.id} type="button" className={o.id === value ? 'on' : ''} disabled={o.disabled}
                title={o.title} onClick={() => onChange(o.id)}>{o.label}</button>
      ))}
    </div>
  );
}

function Library({ strategies, value, onChange }: { strategies: StrategyInfo[]; value: string; onChange: (s: string) => void }) {
  if (!strategies.length) return <div className="sv-note" style={{ marginTop: 12 }}>Loading the library…</div>;
  return (
    <div className="sv-lib">
      {strategies.map((s) => (
        <button key={s.name} type="button" className={`sv-lib-row ${s.name === value ? 'on' : ''}`} onClick={() => onChange(s.name)}>
          <span className="sv-dot" />
          <span>
            <b>{s.title}</b>
            <span className="sv-desc"><span>{s.description}</span></span>
          </span>
          <span className="sv-meta"><span>{s.combos} settings</span>{s.style}</span>
        </button>
      ))}
    </div>
  );
}

function CustomJson({ text, setText, check }: { text: string; setText: (s: string) => void; check: SpecCheck | null }) {
  const [open, setOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  return (
    <div className="sv-custom">
      <div className="sv-steps">
        <div><b>01</b>Copy the prompt below.</div>
        <div><b>02</b>Paste it into ChatGPT or Claude and describe your idea in plain English.</div>
        <div><b>03</b>Paste the JSON it gives you here. We check it before running.</div>
      </div>
      <div className={`sv-prompt ${open ? 'open' : ''}`}>
        <header>
          Sample prompt
          <button type="button" onClick={() => setOpen(!open)}>{open ? 'Collapse' : 'Show all'}</button>
          <button type="button" className={copied ? 'ok' : ''} onClick={async () => {
            try { await navigator.clipboard.writeText(AI_PROMPT + '<' + PROMPT_PLACEHOLDER + '>'); } catch { /* denied */ }
            setCopied(true); setTimeout(() => setCopied(false), 1600);
          }}>{copied ? 'Copied' : 'Copy'}</button>
        </header>
        <pre>{AI_PROMPT}<em>{PROMPT_PLACEHOLDER}</em></pre>
      </div>
      <textarea className="sv-json" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
                placeholder='{ "name": "my_strategy", ... }' />
      <div className="sv-json-tools">
        <span className={`sv-v ${check ? (check.ok ? 'ok' : 'bad') : ''}`}>
          {!check ? 'Paste a strategy to check it'
            : check.ok ? `Valid · ${check.settings} settings · ${check.combos} combinations to try`
              : check.errors[0] + (check.errors.length > 1 ? `  (+${check.errors.length - 1} more)` : '')}
        </span>
        <button type="button" className="sv-link" onClick={() => setText(JSON.stringify(EXAMPLE_SPEC, null, 2))}>Load an example</button>
      </div>
    </div>
  );
}

/* ============================================================ running */

function Running({ market, timeframe, bt, sw, combos }: { market: Market | null; timeframe: string; bt: Job | null; sw: Job | null; combos: number }) {
  const tf = market?.timeframes.find((t) => t.tf === timeframe);
  const queued = !bt || bt.status === 'queued';
  const btDone = bt?.status === 'done';
  const swProg = sw?.status === 'done' ? 1 : sw?.progress ?? 0;
  const n = sw?.result?.combos ?? combos;
  const steps: [string, 'wait' | 'on' | 'done'][] = [
    [queued ? 'Waiting for the worker' : `Loaded ${tf ? tf.bars.toLocaleString() : ''} bars of ${market?.symbol} ${timeframe}`,
      queued ? 'on' : 'done'],
    ['Backtested the chosen settings on train and test', btDone ? 'done' : queued ? 'wait' : 'on'],
    [`Tried ${n} combinations × 2 windows`, sw?.status === 'done' ? 'done' : sw?.status === 'running' ? 'on' : 'wait'],
    ['Ranked them on the test window only', sw?.status === 'done' ? 'done' : 'wait'],
  ];
  return (
    <div className="sv-running">
      <Trace />
      <div className="sv-count"><span>{Math.round(n * swProg)}</span><small> / {n}</small></div>
      <div className="sv-count-label">setting combinations tested on train and test</div>
      <div className="sv-bar"><div style={{ width: `${Math.max(swProg * 100, btDone ? 30 : queued ? 4 : 15)}%` }} /></div>
      <ul className="sv-steps-run">
        {steps.map(([t, s]) => <li key={t} className={s}><i />{t}</li>)}
      </ul>
    </div>
  );
}

/* A line that draws itself while the worker runs. Runs only in this phase. */
function Trace() {
  const [d, setD] = useState({ line: '', fill: '', head: [0, 110] as [number, number] });
  useEffect(() => {
    const pts: [number, number][] = [];
    let y = 110, raf = 0;
    const t0 = performance.now();
    const tick = (now: number) => {
      const x = Math.min(640, (now - t0) / 4.2);
      while (pts.length < x / 4) { y = clamp(y - (Math.random() - 0.42) * 9, 18, 138); pts.push([pts.length * 4, y]); }
      const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0]} ${p[1].toFixed(1)}`).join(' ');
      const last = pts[pts.length - 1] ?? [0, y];
      setD({ line, fill: pts.length > 1 ? `${line} L ${last[0]} 150 L 0 150 Z` : '', head: last });
      if (x < 640) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, []);
  return (
    <svg className="sv-trace" viewBox="0 0 640 150" preserveAspectRatio="none" aria-hidden="true">
      <defs>
        <linearGradient id="svtg" x1="0" x2="1"><stop offset="0" stopColor="#3f9e74" stopOpacity="0" /><stop offset="1" stopColor="#9fe3c2" /></linearGradient>
        <linearGradient id="svtf" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stopColor="#3f9e74" stopOpacity=".22" /><stop offset="1" stopColor="#3f9e74" stopOpacity="0" /></linearGradient>
      </defs>
      <path d={d.fill} fill="url(#svtf)" />
      <path d={d.line} fill="none" stroke="url(#svtg)" strokeWidth="1.8" />
      {d.line && <circle cx={d.head[0]} cy={d.head[1]} r="3.5" fill="#dff7ea" />}
    </svg>
  );
}

/* ============================================================ results */

type ResultsProps = {
  title: string; bt: Job; sw: Job | null; view: Window; setView: (w: Window) => void;
  chosen: number | null; rerunning: boolean; onUse: (r: SweepRow) => void; onBack: () => void; error: string | null;
  labels: Record<string, string>;
};

function Results({ title, bt, sw, view, setView, chosen, rerunning, onUse, onBack, error, labels }: ResultsProps) {
  const r = bt.result!;
  const m = r.metrics![view];
  const w = r.windows;
  const range = view === 'test' ? [w.test.start, w.test.end] : view === 'train' ? [w.train.start, w.train.end] : [w.train.start, w.test.end];
  const sr = sw?.status === 'done' ? sw.result : null;
  const rows = sr?.top ?? [];
  const varied = rows.length ? Object.keys(rows[0].params).filter((k) => rows.some((x) => x.params[k] !== rows[0].params[k])) : [];
  // Short column heads: the spec's label without its parenthetical.
  const labelOf = (k: string) => (labels[k] ?? k.replace(/_/g, ' ')).replace(/ \(.*\)/, '').replace(/^Minimum trend strength/, 'Min. trend');

  return (
    <div className="sv-results">
      <div className="sv-rtop">
        <button type="button" className="sv-back" onClick={onBack}>
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6"><path d="M10 3L5 8l5 5" /></svg>
          New test
        </button>
        <div className="sv-crumb"><b>{title || r.strategy}</b> · {r.symbol} · {r.timeframe} · data through {date(r.as_of)}
          {r.adjust !== 'none' ? ` · ${r.adjust}-adjusted` : ''}</div>
        <span className="sv-chip" title="The Robust / Weak / Overfit verdict arrives with the verdict engine">Unverified</span>
        <Tabs value={view} onChange={setView} />
      </div>

      <div className={`sv-hero ${rerunning ? 'dim' : ''}`}>
        <div>
          <div className="sv-hlabel">Return on {view === 'test' ? 'the test window' : view === 'train' ? 'the training window' : 'train + test'} · {date(range[0])} – {date(range[1])}</div>
          <CountUp key={`${bt.id}-${view}`} className={`sv-big ${(m.net_pct ?? 0) >= 0 ? 'good' : 'bad'}`} value={m.net_pct ?? 0} />
          <div className="sv-vs">vs <span className="mono">{pct(m.bh_pct)}</span> buy &amp; hold · {chosen ? `settings #${chosen}` : 'chosen settings'}</div>
          <p className="sv-say">{sentence(m, view)}</p>
        </div>
        <div className="sv-stats">
          <Stat label="Max drawdown" value={m.max_dd_pct == null ? '—' : `−${m.max_dd_pct.toFixed(1)}%`} tone="bad" />
          <Stat label="Sharpe" value={m.sharpe == null ? '—' : m.sharpe.toFixed(2)} />
          <Stat label="Trades" value={String(m.trades)} />
          <Stat label="Win rate" value={m.win_pct == null ? '—' : `${m.win_pct.toFixed(0)}%`} />
          <Stat label="Profit factor" value={m.profit_factor == null ? '—' : m.profit_factor.toFixed(2)} />
        </div>
      </div>
      {m.ruin && <div className="sv-warnline bad">This run lost the whole account. Everything after that point is flat at zero.</div>}
      {w.thin_sample && <div className="sv-warnline">Less than two years of data. Treat any result here as thin evidence.</div>}
      {error && <div className="sv-warnline bad">{error}</div>}

      {bt.curve && <EquityChart curve={bt.curve} trainEnd={w.test.start} animateKey={String(bt.id)} />}

      <section className="sv-section">
        <div className="sv-sech"><h3>Best settings</h3>
          <p>{sr ? `All ${sr.combos} combinations, ranked on the test window. Train rank alongside, because the two disagreeing is the warning sign.`
            : sw?.status === 'failed' ? `The sweep failed: ${sw.error}` : 'Still ranking…'}</p></div>
        {sr && (
          <div className="sv-best">
            <div className="sv-tblwrap">
              <table className="sv-t">
                <thead><tr><th>#</th>{varied.map((k) => <th key={k}>{labelOf(k)}</th>)}<th>Test return</th><th>Train rank</th><th>Trades</th><th /></tr></thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.test_rank} className={chosen === row.test_rank ? 'sel' : ''}>
                      <td className="mono">{row.test_rank}</td>
                      {varied.map((k) => <td key={k} className="mono">{row.params[k]}</td>)}
                      <td className={`mono ${(row.test.net_pct ?? 0) >= 0 ? 'good' : 'bad'}`}>{pct(row.test.net_pct)}{row.test.ruin ? ' · ruin' : ''}</td>
                      <td className={`mono ${row.train_rank > row.test_rank + 8 ? 'warn' : ''}`}>{row.train_rank} <span className="dim">of {sr.combos}</span></td>
                      <td className="mono">{row.test.trades}</td>
                      <td><button type="button" className="sv-use" disabled={rerunning} onClick={() => onUse(row)}>
                        {chosen === row.test_rank ? (rerunning ? 'Running…' : 'Using') : 'Use'}</button></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {sr.ranks && <RankScatter ranks={sr.ranks} combos={sr.combos ?? 0} chosen={chosen} corr={sr.rank_corr ?? null} />}
          </div>
        )}
      </section>

      {bt.trades && <TradeList trades={bt.trades} testStart={w.test.start} />}

      <div className="sv-costnote">
        Every number above already pays <b>{m.costs?.commission_pct ?? 0}% commission + {m.costs?.slippage_bps ?? 0} bps slippage</b> on
        both sides of every trade: about {money((m.fees ?? 0) + (m.slippage ?? 0))} on a $10k start
        ({(m.cost_pct_of_capital ?? 0).toFixed(0)}% of it). Stops the price gaps through fill at the gap, not at the stop.
      </div>
    </div>
  );
}

function Tabs({ value, onChange }: { value: Window; onChange: (w: Window) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [ul, setUl] = useState({ left: 0, width: 0 });
  useLayoutEffect(() => {
    const on = ref.current?.querySelector<HTMLButtonElement>('button.on');
    if (on) setUl({ left: on.offsetLeft, width: on.offsetWidth });
  }, [value]);
  return (
    <div className="sv-wtabs" ref={ref}>
      {(['train', 'test', 'full'] as Window[]).map((v) => (
        <button key={v} type="button" className={v === value ? 'on' : ''} onClick={() => onChange(v)}>
          {v === 'full' ? 'Train + test' : v[0].toUpperCase() + v.slice(1)}
        </button>
      ))}
      <span className="sv-ul" style={{ left: ul.left, width: ul.width }} />
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

function TradeList({ trades, testStart }: { trades: Trade[]; testStart: number }) {
  const [all, setAll] = useState(false);
  const recent = [...trades].reverse();
  const shown = all ? recent : recent.slice(0, 14);
  return (
    <section className="sv-section">
      <div className="sv-sech"><h3>Trades</h3><p>{trades.length} over train + test · newest first</p></div>
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
                <td className="dim">{t.entry_ts >= testStart ? 'test' : 'train'}</td>
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

/* -- words and numbers ---------------------------------------------------------- */

function sentence(m: Metrics, view: Window): string {
  const where = view === 'train' ? 'the training window' : view === 'test' ? 'data it never saw' : 'train and test together';
  if (m.ruin) return `Lost the entire account on ${where}. Nothing else here matters.`;
  const net = m.net_pct ?? 0, bh = m.bh_pct ?? 0;
  if (net < 0) return `Lost ${pct(net, false)} on ${where}, while simply holding made ${pct(bh)}.`;
  if (net > bh) return `Beat buy-and-hold by ${Math.round(net - bh).toLocaleString()} points on ${where}, with a worst drop of ${(m.max_dd_pct ?? 0).toFixed(0)}% along the way.`;
  return `Made ${pct(net)} on ${where}, but simply holding made ${pct(bh)}.`;
}

function pct(v: number | null | undefined, sign = true): string {
  if (v == null || !Number.isFinite(v)) return '—';
  const a = Math.abs(v);
  const s = a.toLocaleString('en-US', { maximumFractionDigits: a >= 100 ? 0 : 1 });
  return `${sign ? (v >= 0 ? '+' : '−') : ''}${s}%`;
}
function date(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}
function price(v: number): string {
  return v >= 1000 ? Math.round(v).toLocaleString('en-US') : v >= 10 ? v.toFixed(2) : v.toFixed(4);
}
function clamp(v: number, a: number, b: number) { return Math.max(a, Math.min(b, v)); }
