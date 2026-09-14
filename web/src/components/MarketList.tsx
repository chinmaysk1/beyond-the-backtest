'use client';

import { useMemo, useState } from 'react';

import type { Market } from '../lib/types';

type Props = {
  markets: Market[];
  selected: string | null;
  onSelect: (m: Market) => void;
};

const FILTERS = [
  { id: 'all', label: 'All' },
  { id: 'crypto', label: 'Crypto' },
  { id: 'equity', label: 'Equity' },
];

export default function MarketList({ markets, selected, onSelect }: Props) {
  const [filter, setFilter] = useState('all');
  const [expanded, setExpanded] = useState<string | null>(selected);

  const shown = useMemo(
    () => markets.filter((m) => filter === 'all' || m.assetClass === filter),
    [markets, filter],
  );

  return (
    <section className="panel markets">
      <header><h2>Markets</h2></header>

      <div className="chips">
        {FILTERS.map((f) => (
          <button
            key={f.id}
            type="button"
            className={`chip ${filter === f.id ? 'on' : ''}`}
            onClick={() => setFilter(f.id)}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="mlist">
        {!markets.length && <div className="state">loading markets…</div>}

        {shown.map((m) => {
          const on = m.symbol === selected;
          const open = expanded === m.symbol;
          return (
            <div key={m.symbol} className={`mrow ${on ? 'on' : ''}`}>
              <div
                className="top"
                role="button"
                tabIndex={0}
                onClick={() => {
                  if (on) setExpanded(open ? null : m.symbol);
                  else { onSelect(m); setExpanded(m.symbol); }
                }}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    if (on) setExpanded(open ? null : m.symbol);
                    else { onSelect(m); setExpanded(m.symbol); }
                  }
                }}
              >
                <div className="tick">{m.symbol.replace('/USD', '').slice(0, 4)}</div>
                <div className="who">
                  <div className="sym">{m.symbol}</div>
                  <div className="meta">
                    {m.name}{m.firstTs ? ` · since ${year(m.firstTs)}` : ''}
                  </div>
                </div>
                <svg
                  className="chev"
                  style={{ transform: open ? 'rotate(180deg)' : undefined }}
                  width="12" height="12" viewBox="0 0 16 16"
                  fill="none" stroke="currentColor" strokeWidth="1.5"
                >
                  <path d="M4 6.5 8 10.5l4-4" />
                </svg>
              </div>

              {open && (
                <div className="tfs">
                  <table className="tf-table">
                    <thead>
                      <tr><th>TF</th><th>Bars</th><th>Span</th></tr>
                    </thead>
                    <tbody>
                      {m.timeframes.map((t) => (
                        <tr key={t.tf}>
                          <td className="tfname">{t.tf}</td>
                          <td className="mono">{t.bars.toLocaleString()}</td>
                          <td className="mono" style={{ color: 'var(--text-3)' }}>
                            {span(t.firstTs, t.lastTs)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}

function year(ts: number) {
  return new Date(ts * 1000).getUTCFullYear();
}

/* How much history a series actually holds.
 *
 * Shown as an elapsed span rather than a start date, because the number that
 * matters is "how much is there" -- equity intraday reads 53d here, which is
 * the honest answer and the reason those series are not research-grade.
 */
function span(first: number | null, last: number | null) {
  if (!first || !last) return '—';
  const days = (last - first) / 86400;
  if (days < 400) return `${Math.round(days)}d`;
  return `${(days / 365.25).toFixed(days < 3650 ? 1 : 0)}y`;
}
