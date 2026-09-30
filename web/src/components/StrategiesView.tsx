'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { AI_PROMPT, EXAMPLE_SPEC, PROMPT_PLACEHOLDER, checkSpecText, type SpecCheck } from '../lib/specCheck';
import { clamp, date } from '../lib/format';
import { pollJob, submitJob } from '../lib/jobs';
import type { Costs, CustomStrategyInfo, Job, Market, StrategyInfo } from '../lib/types';

/* The Strategies tab, as one flow in two phases:
 *
 *   compose   one question, centred: which market, which timeframe, which
 *             strategy (from the library, or your own JSON), at what cost
 *   running   the backtest and its sweep, live from the worker
 *
 * Then the test opens in the Results tab (TestView), where every test lives:
 * the return on data it never saw, the verdict, the curve, every setting the
 * sweep tried and how much to trust the best one.
 *
 * "Run backtest" always queues the sweep too. Finding the best settings is not
 * a separate decision the user has to know to make -- and a best-settings list
 * without the train/test comparison beside it is exactly the misleading
 * number this product exists to replace.
 */

type Phase = 'compose' | 'running';

const ALL_TF = ['5m', '15m', '30m', '1h', '4h', '1d'];
// Mirrors btb/engine/fills.py DEFAULT_COSTS. The form starts here; the engine
// applies its own defaults if none are sent.
const DEFAULT_COSTS: Record<string, Costs> = {
  crypto: { commission_pct: 0.1, slippage_bps: 5 },
  equity: { commission_pct: 0, slippage_bps: 2 },
};
const MIN_RUN_MS = 2200;      // long enough to read what happened, never padded past that

export type Opened = { id: number; sweepId: number | null };

/* What was last composed, so coming back from a test's page to run another
 * starts where the last one left off. Module state: it outlives the tab. */
let last: { symbol: string | null; timeframe: string; source: 'lib' | 'json'; strategy: string;
            jsonText: string; costs: Costs } | null = null;

export default function StrategiesView({ markets, onDone }: { markets: Market[]; onDone: (t: Opened) => void }) {
  const router = useRouter();
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [custom, setCustom] = useState<CustomStrategyInfo[]>([]);
  const [symbol, setSymbol] = useState<string | null>(last?.symbol ?? null);
  const [timeframe, setTimeframe] = useState(last?.timeframe ?? '4h');
  const [source, setSource] = useState<'lib' | 'json'>(last?.source ?? 'lib');
  const [strategy, setStrategy] = useState(last?.strategy ?? 'ema_cross_adx');
  const [jsonText, setJsonText] = useState(last?.jsonText ?? '');
  const [costs, setCosts] = useState<Costs>(last?.costs ?? DEFAULT_COSTS.crypto);
  const [costsOpen, setCostsOpen] = useState(false);

  const [phase, setPhase] = useState<Phase>('compose');
  const [bt, setBt] = useState<Job | null>(null);
  const [sw, setSw] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Set on mount as well as cleared on unmount: React's development mode
  // mounts, unmounts and remounts every component once, and a flag that is only
  // ever cleared stays false after that -- silently dropping every update.
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; };
  }, []);

  useEffect(() => {
    last = { symbol, timeframe, source, strategy, jsonText, costs };
  }, [symbol, timeframe, source, strategy, jsonText, costs]);

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
    refreshCustom();
  }, [router]);

  // Past JSON strategies change whenever one runs, so unlike the library this
  // is fetched fresh each time. Not having them is not worth an error.
  function refreshCustom() {
    fetch('/api/strategies/custom')
      .then((res) => (res.ok ? res.json() : null))
      .then((out) => { if (out && alive.current) setCustom(out.strategies ?? []); })
      .catch(() => {});
  }

  // A past spec goes back through the JSON box, so it is checked and run
  // exactly as if it had just been pasted.
  const pickCustom = useCallback((c: CustomStrategyInfo) => {
    setJsonText(JSON.stringify(c.spec, null, 2));
    setSource('json');
  }, []);

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

  /* -- run ----------------------------------------------------------------- */

  // Queue the backtest and its sweep, show them working, then hand the test
  // to the Results tab -- its page is where every result lives.
  async function run() {
    if (!canRun) return;
    setError(null);
    setBt(null);
    setSw(null);
    const started = performance.now();
    const body: Record<string, unknown> = { kind: 'backtest', withSweep: true, symbol, timeframe, costs };
    if (source === 'json' && check?.ok) body.spec = check.spec;
    else body.strategy = strategy;
    let ids;
    try {
      ids = await submitJob(body);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    setPhase('running');
    if (source === 'json') refreshCustom();
    const live = () => alive.current;
    try {
      const [b] = await Promise.all([pollJob(ids.id, setBt, live), ids.sweepId ? pollJob(ids.sweepId, setSw, live) : null]);
      if (b.status !== 'done') {
        setError(b.error ?? 'The backtest failed');
        setPhase('compose');
        return;
      }
      const wait = MIN_RUN_MS - (performance.now() - started);
      if (wait > 0) await new Promise((r) => setTimeout(r, wait));
      if (alive.current) onDone({ id: ids.id, sweepId: ids.sweepId });
    } catch (e) {
      if ((e as Error).message === 'signed out') router.replace('/login');
    }
  }

  /* -- render -------------------------------------------------------------- */

  return (
    <div className="sv">
      <div className={`sv-phase ${phase === 'compose' ? 'on' : ''}`}>
        <Compose
          markets={markets} market={market} pickMarket={pickMarket}
          timeframe={timeframe} setTimeframe={setTimeframe}
          source={source} setSource={setSource}
          strategies={strategies} strategy={strategy} setStrategy={setStrategy}
          custom={custom} pickCustom={pickCustom}
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
  custom: CustomStrategyInfo[]; pickCustom: (c: CustomStrategyInfo) => void;
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
                ? <Library strategies={p.strategies} custom={p.custom} value={p.strategy} onChange={p.setStrategy} onCustom={p.pickCustom} />
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

type LibraryProps = {
  strategies: StrategyInfo[]; custom: CustomStrategyInfo[];
  value: string; onChange: (s: string) => void; onCustom: (c: CustomStrategyInfo) => void;
};

/* The library, then the user's own past JSON strategies under it. One search
 * box filters both. Picking a past one opens it in Custom JSON to run again. */
function Library({ strategies, custom, value, onChange, onCustom }: LibraryProps) {
  const [q, setQ] = useState('');
  if (!strategies.length) return <div className="sv-note" style={{ marginTop: 12 }}>Loading the library…</div>;
  const needle = q.trim().toLowerCase();
  const match = (s: StrategyInfo) => `${s.name} ${s.title} ${s.description} ${s.style}`.toLowerCase().includes(needle);
  const lib = strategies.filter(match);
  const mine = custom.filter(match);
  return (
    <div className="sv-lib">
      <input className="sv-lib-search" value={q} onChange={(e) => setQ(e.target.value)}
             placeholder={`Search ${strategies.length + custom.length} strategies`}
             onKeyDown={(e) => {
               if (e.key === 'Enter') { if (lib[0]) onChange(lib[0].name); else if (mine[0]) onCustom(mine[0]); }
               if (e.key === 'Escape') setQ('');
             }} />
      {!lib.length && !mine.length && <div className="sv-note">Nothing matches “{q.trim()}”.</div>}
      {custom.length > 0 && lib.length > 0 && <h6>Library</h6>}
      {lib.map((s) => (
        <button key={s.name} type="button" className={`sv-lib-row ${s.name === value ? 'on' : ''}`} onClick={() => onChange(s.name)}>
          <span className="sv-dot" />
          <span>
            <b>{s.title}</b>
            <span className="sv-desc"><span>{s.description}</span></span>
          </span>
          <span className="sv-meta"><span>{s.combos} settings</span>{s.style}</span>
        </button>
      ))}
      {mine.length > 0 && <h6>Custom</h6>}
      {mine.map((c) => (
        <button key={c.id} type="button" className="sv-lib-row" title="Open in Custom JSON to run it again" onClick={() => onCustom(c)}>
          <span className="sv-dot" />
          <span>
            <b>{c.title}</b>
            {c.description && <span className="sv-lib-sub">{c.description}</span>}
          </span>
          <span className="sv-meta"><span>{c.combos} settings</span>ran {date(Date.parse(c.lastRun) / 1000)}</span>
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
