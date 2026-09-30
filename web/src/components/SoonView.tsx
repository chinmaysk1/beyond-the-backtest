import type { RoomId } from '../lib/rooms';

/* The sections that exist in the product's shape but not yet in code. Said
 * plainly, with when -- not dressed up with placeholder charts. */
const COPY: Partial<Record<RoomId, { title: string; body: string; when: string; icon: string }>> = {
  paper: {
    title: 'Paper trading', when: 'Weeks 9–12',
    body: 'Run a strategy that passed on live prices with pretend money, before any real money is involved.',
    icon: 'M3 12h4l3-8 4 16 3-8h4',
  },
};

export default function SoonView({ room }: { room: RoomId }) {
  const c = COPY[room];
  if (!c) return null;
  return (
    <div className="soon">
      <div>
        <div className="soon-glyph">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d={c.icon} /></svg>
        </div>
        <h2>{c.title}</h2>
        <p>{c.body}</p>
        <span className="soon-when">{c.when}</span>
      </div>
    </div>
  );
}
