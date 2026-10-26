'use client';

import { useRouter } from 'next/navigation';
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { clamp, date } from '../lib/format';
import { pollJob, submitJob } from '../lib/jobs';
import { describeCosts, messageText, type PineCheck, type PineInput } from '../lib/pine';
import { PINE_REFERENCE } from '../lib/pineReference';
import type { Costs, CustomStrategyInfo, Job, Market, StrategyInfo } from '../lib/types';
import ConnectAI from './ConnectAI';

/* The Strategies tab, as one flow in two phases:
 *
 *   compose   one question, centred: which market, which timeframe, which
 *             strategy (from the library, your own Pine, or written by your own
 *             AI over MCP), at what cost
 *   running   the backtest and its sweep, live from the worker
 *
 * Then the test opens in the Results tab (TestView), where every test lives:
 * the return on data it never saw, the score, the curve, every setting the
 * sweep tried and how much to trust the best one.
 *
 * "Run backtest" always queues the sweep too. Finding the best settings is not
 * a separate decision the user has to know to make -- and a best-settings list
 * without the train/test comparison beside it is exactly the misleading
 * number this product exists to replace. A script with no // @grid lines has
 * nothing to sweep: it runs once and its card says it is unscored.
 */

type Phase = 'compose' | 'running';
type Source = 'lib' | 'pine' | 'ai';

const ALL_TF = ['5m', '15m', '30m', '1h', '4h', '1d'];
// Mirrors btb/engine/fills.py DEFAULT_COSTS. These are what a script that sets
// no costs of its own is charged; a script that sets any is charged its own.
const DEFAULT_COSTS: Record<string, Costs> = {
  crypto: { commission_pct: 0.1, slippage_bps: 5 },
  equity: { commission_pct: 0, slippage_bps: 2 },
};
const MIN_RUN_MS = 2200;      // long enough to read what happened, never padded past that
const CHECK_DELAY_MS = 700;   // after the last keystroke

export const EXAMPLE_PINE = PINE_REFERENCE.split('## 6. A complete example')[1]
  ?.split('```pine')[1]?.split('```')[0]?.trim() ?? '';
export const PROMPT_PLACEHOLDER = 'describe your idea in plain English here';
export const AI_PROMPT = `${PINE_REFERENCE}

---

Write a strategy for Beyond the Backtest that follows every rule above. Reply with the
Pine script only, in one code block.

My idea: `;

export type Opened = { id: number; sweepId: number | null };

/* What was last composed, so coming back from a test's page to run another
 * starts where the last one left off. Module state: it outlives the tab. */
let last: { symbol: string | null; timeframe: string; source: Source; strategy: string;
            pineText: string; costs: Costs } | null = null;

export default function StrategiesView({ markets, onDone }: { markets: Market[]; onDone: (t: Opened) => void }) {
  const router = useRouter();
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  const [custom, setCustom] = useState<CustomStrategyInfo[]>([]);
  const [symbol, setSymbol] = useState<string | null>(last?.symbol ?? null);
  const [timeframe, setTimeframe] = useState(last?.timeframe ?? '4h');
  const [source, setSource] = useState<Source>(last?.source === 'ai' ? 'lib' : last?.source ?? 'lib');
  const [strategy, setStrategy] = useState(last?.strategy ?? 'ema_cross_adx');
  const [pineText, setPineText] = useState(last?.pineText ?? '');
  const [costs, setCosts] = useState<Costs>(last?.costs ?? DEFAULT_COSTS.crypto);
  const [costsOpen, setCostsOpen] = useState(false);
  const check = usePineCheck(source === 'pine' ? pineText : '');

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
    last = { symbol, timeframe, source, strategy, pineText, costs };
  }, [symbol, timeframe, source, strategy, pineText, costs]);

  const market = useMemo(() => markets.find((m) => m.symbol === symbol) ?? null, [markets, symbol]);
  const spec = strategies.find((s) => s.name === strategy) ?? null;
  const ok = check.result?.ok === true;
  const combos = source === 'lib' ? spec?.combos ?? 0 : ok ? check.result!.grid.combinations : 0;
  const scored = source === 'lib' ? combos > 0 : ok && check.result!.scored;
  const scriptCosts = source === 'pine' && ok && check.result!.costs.set ? check.result!.costs : null;
  const canRun = !!symbol && (source === 'lib' ? !!spec : source === 'pine' && ok && !check.busy);

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

  // Past scripts change whenever one runs, so unlike the library this is
  // fetched fresh each time. Not having them is not worth an error.
  function refreshCustom() {
    fetch('/api/strategies/custom')
      .then((res) => (res.ok ? res.json() : null))
      .then((out) => { if (out && alive.current) setCustom(out.strategies ?? []); })
      .catch(() => {});
  }

  // A past script, or a library one to edit, goes back through the Pine box,
  // so it is checked and run exactly as if it had just been pasted.
  const editPine = useCallback((text: string | undefined) => {
    if (!text) return;
    setPineText(text);
    setSource('pine');
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
    if (source === 'pine') body.source = pineText;
    else body.strategy = strategy;
    let ids;
    try {
      ids = await submitJob(body);
    } catch (e) {
      setError((e as Error).message);
      return;
    }
    setPhase('running');
    if (source === 'pine') refreshCustom();
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
          custom={custom} editPine={editPine}
          pineText={pineText} setPineText={setPineText} check={check}
          costs={costs} setCosts={setCosts} costsOpen={costsOpen} setCostsOpen={setCostsOpen}
          scriptCosts={scriptCosts}
          combos={combos} scored={scored} canRun={canRun} onRun={run} error={error}
        />
      </div>

      <div className={`sv-phase ${phase === 'running' ? 'on' : ''}`}>
        {phase === 'running' && (
          <Running market={market} timeframe={timeframe} bt={bt} sw={sw} combos={combos} scored={scored} />
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

/* Check the script CHECK_DELAY_MS after the last keystroke. The checker is the
 * engine's own (btb/pine/check.py, through a check job), so what this panel
 * says is exactly what a run will be held to. A newer keystroke wins: an
 * answer for text that has since changed is dropped. */
type CheckState = { result: PineCheck | null; busy: boolean; error: string | null };
function usePineCheck(text: string): CheckState {
  const [state, setState] = useState<CheckState>({ result: null, busy: false, error: null });
  const latest = useRef('');
  useEffect(() => {
    latest.current = text;
    if (!text.trim()) { setState({ result: null, busy: false, error: null }); return; }
    setState((s) => ({ ...s, busy: true }));
    const t = setTimeout(async () => {
      try {
        const res = await fetch('/api/pine/check', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ source: text }),
        });
        const out = await res.json();
        if (latest.current !== text) return;
        if (!res.ok) setState({ result: null, busy: false, error: out.error ?? 'Could not check the script' });
        else setState({ result: out as PineCheck, busy: false, error: null });
      } catch {
        if (latest.current === text) setState({ result: null, busy: false, error: 'Could not reach the checker' });
      }
    }, CHECK_DELAY_MS);
    return () => clearTimeout(t);
  }, [text]);
  return state;
}

/* ============================================================ compose */

type ComposeProps = {
  markets: Market[]; market: Market | null; pickMarket: (m: Market) => void;
  timeframe: string; setTimeframe: (t: string) => void;
  source: Source; setSource: (s: Source) => void;
  strategies: StrategyInfo[]; strategy: string; setStrategy: (s: string) => void;
  custom: CustomStrategyInfo[]; editPine: (text: string | undefined) => void;
  pineText: string; setPineText: (s: string) => void; check: CheckState;
  costs: Costs; setCosts: (c: Costs) => void; costsOpen: boolean; setCostsOpen: (b: boolean) => void;
  scriptCosts: PineCheck['costs'] | null;
  combos: number; scored: boolean; canRun: boolean; onRun: () => void; error: string | null;
};

function Compose(p: ComposeProps) {
  const have = new Set(p.market?.timeframes.map((t) => t.tf) ?? []);
  const tfInfo = p.market?.timeframes.find((t) => t.tf === p.timeframe);
  const reason = (tf: string) => (p.market?.assetClass === 'equity' && tf === '4h'
    ? 'Yahoo does not serve 4h bars for stocks' : 'Not stored for this market yet');

  return (
    <div className="sv-compose">
      <div className={`sv-composer ${p.source === 'pine' ? 'wide' : ''}`}>
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
              <Seg value={p.source} onChange={(v) => p.setSource(v as Source)}
                   options={[{ id: 'lib', label: 'Library' }, { id: 'pine', label: 'Your Pine' },
                             { id: 'ai', label: 'Connect your AI' }]} />
              {p.source === 'lib' && <Library strategies={p.strategies} custom={p.custom} value={p.strategy}
                                              onChange={p.setStrategy} onEdit={p.editPine} />}
              {p.source === 'pine' && <PineEditor text={p.pineText} setText={p.setPineText} check={p.check} />}
              {p.source === 'ai' && <ConnectAI prompt={AI_PROMPT} placeholder={PROMPT_PLACEHOLDER} />}
            </div>
          </div>

          {p.source !== 'ai' && <>
            <div className="sv-field">
              <label>Costs</label>
              <div>
                {p.scriptCosts ? (
                  <div className="sv-costs">
                    <span>{describeCosts(p.scriptCosts)}</span>
                    {p.scriptCosts.zero && <span className="sv-warn-chip">optimistic: real fills cost more</span>}
                  </div>
                ) : <>
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
                  <div className="sv-note">Charged when the script sets no costs of its own.</div>
                </>}
              </div>
            </div>

            <button type="button" className="sv-run" disabled={!p.canRun} onClick={p.onRun}>Run backtest</button>
            <div className="sv-run-sub">
              {p.canRun
                ? p.scored
                  ? <>Runs the chosen settings, then tries all <b>{p.combos}</b> combinations and ranks them on data they never saw.</>
                  : <>Runs the script once on its defaults. With no <span className="mono">// @grid</span> lines there is nothing to sweep, so it won’t be scored.</>
                : p.source === 'pine' ? (p.check.busy ? 'Checking…' : 'Paste a script that checks out to continue.')
                  : 'Choose a market and a strategy.'}
            </div>
            {p.error && <div className="sv-error">{p.error}</div>}
          </>}
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
  value: string; onChange: (s: string) => void; onEdit: (text: string | undefined) => void;
};

/* The library, then the user's own past scripts under it. One search box
 * filters both. Picking a past one opens it in Your Pine to run again; a
 * library one can be opened there to edit. */
function Library({ strategies, custom, value, onChange, onEdit }: LibraryProps) {
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
               if (e.key === 'Enter') { if (lib[0]) onChange(lib[0].name); else if (mine[0]) onEdit(mine[0].source); }
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
            {s.name === value && s.source && (
              <span className="sv-link sv-edit" role="button" tabIndex={0}
                    onClick={(e) => { e.stopPropagation(); onEdit(s.source); }}
                    onKeyDown={(e) => { if (e.key === 'Enter') { e.stopPropagation(); onEdit(s.source); } }}>
                Edit as Pine
              </span>
            )}
          </span>
          <span className="sv-meta"><span>{s.combos} settings</span>{s.style}</span>
        </button>
      ))}
      {mine.length > 0 && <h6>Yours</h6>}
      {mine.map((c) => (
        <button key={c.id} type="button" className="sv-lib-row" title="Open in Your Pine to run it again" onClick={() => onEdit(c.source)}>
          <span className="sv-dot" />
          <span>
            <b>{c.title}</b>
            {c.description && <span className="sv-lib-sub">{c.description}</span>}
          </span>
          <span className="sv-meta"><span>{c.combos ? `${c.combos} settings` : 'unscored'}</span>ran {date(Date.parse(c.lastRun) / 1000)}</span>
        </button>
      ))}
    </div>
  );
}

/* The inputs worth listing: numbers (what a sweep can try) and dates. */
function shownInput(i: PineInput): boolean {
  return !!i.name && (i.type === 'int' || i.type === 'float' || i.type === 'time');
}

/* An input's default as a person reads it. A time input is milliseconds since
 * the epoch, shown as its UTC date (and time, when it is not midnight). */
function inputValue(i: PineInput): string {
  if (i.type === 'time' && typeof i.default === 'number') {
    const iso = new Date(i.default).toISOString();
    return iso.endsWith('T00:00:00.000Z') ? iso.slice(0, 10) : `${iso.slice(0, 16).replace('T', ' ')} UTC`;
  }
  return String(i.default);
}

/* A plain textarea and, beside it, what the engine's checker says about it:
 * the errors with their lines, then -- once it checks out -- the inputs and
 * the values the sweep will try, N, and the costs it will be charged. */
function PineEditor({ text, setText, check }: { text: string; setText: (s: string) => void; check: CheckState }) {
  const [copied, setCopied] = useState(false);
  const r = check.result;
  return (
    <div className="sv-custom sv-pine">
      <div className="sv-pine-grid">
        <textarea className="sv-json sv-src" spellCheck={false} value={text} onChange={(e) => setText(e.target.value)}
                  placeholder={'//@version=6\nstrategy("My idea", overlay=true, ...)\n\nlen = input.int(20, "Length")\n// @grid len = 10, 20, 40\n...'} />
        <div className="sv-check" aria-live="polite">
          {!text.trim() ? (
            <div className="sv-check-empty">
              Paste a Pine v6 strategy from TradingView, or one your AI wrote. Add
              <span className="mono"> // @grid </span>lines to say which settings to try.
            </div>
          ) : check.busy && !r ? <div className="sv-v">Checking…</div>
            : check.error ? <div className="sv-v bad">{check.error}</div>
            : r && !r.ok ? (
              <>
                <div className="sv-v bad">{r.errors.length} problem{r.errors.length > 1 ? 's' : ''} to fix</div>
                <ul className="sv-errs">{r.errors.slice(0, 8).map((m, i) => <li key={i}>{messageText(m)}</li>)}</ul>
              </>
            ) : r ? (
              <>
                <div className={`sv-v ${check.busy ? '' : 'ok'}`}>{check.busy ? 'Checking…' : `Checks out · ${r.title || 'Pine v6 strategy'}`}</div>
                <div className="sv-check-n">
                  <b>{r.scored ? r.grid.combinations : 1}</b>
                  <small>{r.scored ? 'settings will be tried' : 'setting: no // @grid lines, so not scored'}</small>
                </div>
                {r.inputs.some(shownInput) && (
                  <table className="sv-inputs">
                    <thead><tr><th>Input</th><th>Default</th><th>Swept</th></tr></thead>
                    <tbody>
                      {r.inputs.filter(shownInput).map((i) => (
                        <tr key={i.name!} title={i.title}>
                          <td className="mono">{i.name}</td>
                          <td className="mono">{inputValue(i)}</td>
                          <td className="mono">{r.grid.values[i.name!]?.join(', ') ?? '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <div className="sv-note">{describeCosts(r.costs)}</div>
                {r.warnings.slice(0, 4).map((m, i) => <div key={i} className="sv-warnbox">{messageText(m)}</div>)}
              </>
            ) : null}
        </div>
      </div>
      <div className="sv-json-tools">
        <span className="sv-v">Pine v6 · the same checker a run is held to</span>
        <button type="button" className="sv-link" onClick={() => setText(EXAMPLE_PINE)}>Load an example</button>
        <button type="button" className={`sv-link ${copied ? 'ok' : ''}`} style={{ marginLeft: 0 }} onClick={async () => {
          try { await navigator.clipboard.writeText(AI_PROMPT + '<' + PROMPT_PLACEHOLDER + '>'); } catch { /* denied */ }
          setCopied(true); setTimeout(() => setCopied(false), 1600);
        }}>{copied ? 'Prompt copied' : 'Write it with AI'}</button>
      </div>
    </div>
  );
}

/* ============================================================ running */

function Running({ market, timeframe, bt, sw, combos, scored }: { market: Market | null; timeframe: string; bt: Job | null; sw: Job | null; combos: number; scored: boolean }) {
  const tf = market?.timeframes.find((t) => t.tf === timeframe);
  const queued = !bt || bt.status === 'queued';
  const btDone = bt?.status === 'done';
  const swProg = sw?.status === 'done' ? 1 : sw?.progress ?? 0;
  const n = scored ? sw?.result?.combos ?? combos : 1;
  const steps: [string, 'wait' | 'on' | 'done'][] = [
    [queued ? 'Waiting for the worker' : `Loaded ${tf ? tf.bars.toLocaleString() : ''} bars of ${market?.symbol} ${timeframe}`,
      queued ? 'on' : 'done'],
    ['Backtested the chosen settings on train, test and the holdout', btDone ? 'done' : queued ? 'wait' : 'on'],
    ...(scored ? [
      [`Tried ${n} combinations, each once over train and test`, sw?.status === 'done' ? 'done' : sw?.status === 'running' ? 'on' : 'wait'],
      ['Ranked them on the test window only, and scored the grid', sw?.status === 'done' ? 'done' : 'wait'],
    ] as [string, 'wait' | 'on' | 'done'][] : []),
  ];
  return (
    <div className="sv-running">
      <Trace />
      <div className="sv-count"><span>{Math.round(n * (scored ? swProg : btDone ? 1 : 0))}</span><small> / {n}</small></div>
      <div className="sv-count-label">{scored ? 'setting combinations tested on train and test' : 'setting tested (unscored)'}</div>
      <div className="sv-bar"><div style={{ width: `${Math.max((scored ? swProg : btDone ? 1 : 0) * 100, btDone ? 30 : queued ? 4 : 15)}%` }} /></div>
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
