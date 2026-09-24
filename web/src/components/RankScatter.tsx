/* Every combination a sweep tried, as train rank against test rank.
 *
 * If doing well in training predicted doing well on unseen data, the dots
 * would hug the diagonal. A shapeless cloud is the picture of overfitting --
 * the thing a leaderboard sorted by one number hides. */

type Props = { ranks: [number, number][]; combos: number; chosen: number | null; corr: number | null };

export default function RankScatter({ ranks, combos, chosen, corr }: Props) {
  const P = 26, W = 300, sz = W - P - 8;
  const pos = (r: number) => ((r - 1) / Math.max(combos - 1, 1)) * sz;
  const word = corr == null ? '' : corr > 0.5 ? 'strong' : corr > 0.1 ? 'weak' : corr > -0.1 ? 'none' : 'inverted';
  return (
    <div className="scatter">
      <svg viewBox={`0 0 ${W} ${W + 8}`} role="img" aria-label="Train rank against test rank for every combination">
        <rect x={P} y={8} width={sz} height={sz} fill="none" stroke="rgba(255,255,255,.06)" />
        <line x1={P} y1={8} x2={P + sz} y2={8 + sz} stroke="rgba(255,255,255,.14)" strokeDasharray="3 4" />
        <text x={P + sz / 2} y={W + 6} className="sc-axis" textAnchor="middle">train rank →</text>
        <text x={10} y={8 + sz / 2} className="sc-axis" textAnchor="middle" transform={`rotate(-90 10 ${8 + sz / 2})`}>test rank →</text>
        {ranks.map(([tr, te], i) => {
          const top = te <= 10, sel = chosen === te;
          return (
            <circle key={i} className="sc-dot" style={{ animationDelay: `${i * 8}ms` }}
                    cx={P + pos(tr)} cy={8 + pos(te)} r={sel ? 5.5 : top ? 3.6 : 2.6}
                    fill={sel ? '#e7e9ec' : top ? '#3f9e74' : '#4b535c'}>
              <title>{`train #${tr} · test #${te}`}</title>
            </circle>
          );
        })}
      </svg>
      <p>
        Each dot is one combination. If doing well in training predicted doing well on unseen data,
        the dots would line up along the diagonal. Agreement here: <b className="mono">{corr == null ? '—' : corr.toFixed(2)}</b>{word && `, ${word}`}.
      </p>
    </div>
  );
}
