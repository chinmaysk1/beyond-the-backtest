import type { Band } from '../lib/types';

/* A test's score, 0-100, as a ring that fills to it. The colour is the band
 * from calibration (btb/verdict/rules.py BANDS): high is a score luck alone
 * reaches at most 5% of the time. */
export const BAND_TONE: Record<Band, string> = { high: 'good', mid: 'warn', low: 'bad' };

export default function ScoreRing({ score, band, size = 'md', title }: {
  score: number | null; band: Band | null; size?: 'sm' | 'md' | 'lg'; title?: string;
}) {
  if (score == null || band == null) {
    return <span className={`ring ${size} pending`} title={title}><b>…</b></span>;
  }
  // A sliver even at 0, so an empty ring still reads as a ring.
  const v = Math.max(score, 1.5);
  return (
    <span className={`ring ${size} ${BAND_TONE[band]}`} style={{ ['--v' as string]: v }} title={title}
          role="img" aria-label={`Score ${score} out of 100`}>
      <b>{score}</b>
    </span>
  );
}
