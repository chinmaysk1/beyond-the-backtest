'use client';

import dynamic from 'next/dynamic';
import type { MutableRefObject, Ref } from 'react';

import { ROOMS, type RoomId } from '../lib/rooms';

/* three.js is only ever needed here, so it loads as its own chunk and never
 * blocks the working screens. The space is dark by default, so the wordmark
 * and cards are already readable while it streams in. */
const SpaceScene = dynamic(() => import('./SpaceScene'), { ssr: false });

export type Stage = 'landing' | 'entering' | 'app' | 'leaving';

type Props = {
  stage: Stage;
  onChoose: (room: RoomId) => void;
  cardsRef: MutableRefObject<HTMLButtonElement[]>;
  titleRef: Ref<HTMLHeadingElement>;
};

export default function Landing({ stage, onChoose, cardsRef, titleRef }: Props) {
  return (
    <div className={`landing ${stage}`}>
      <SpaceScene active={stage !== 'app'} warp={stage === 'entering' || stage === 'app'} />
      <div className="vignette" />

      <section className="landing-content">
        <h1 className="wordmark" ref={titleRef}>Beyond the <b>Backtest</b></h1>
        <p className="landing-sub">
          Test a trading idea the honest way: on data it has never seen, with real costs,
          and without letting a lucky setting fool you.
        </p>

        <div className="rooms">
          {ROOMS.map((r, i) => (
            <button
              key={r.id}
              type="button"
              className="room"
              style={{ animationDelay: `${0.55 + i * 0.08}s` }}
              ref={(el) => { if (el) cardsRef.current[i] = el; }}
              onClick={() => onChoose(r.id)}
              onPointerMove={tilt}
              onPointerLeave={(e) => { e.currentTarget.style.transform = ''; }}
              onAnimationEnd={(e) => {
                // A finished entrance animation would otherwise pin `transform`
                // and override the hover tilt.
                e.currentTarget.style.animation = 'none';
                e.currentTarget.style.opacity = '1';
              }}
            >
              <span className="room-ic">
                <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                     strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d={r.icon} /></svg>
              </span>
              <span className="room-title">{r.title}</span>
              <span className="room-desc">{r.desc}</span>
            </button>
          ))}
        </div>
      </section>
    </div>
  );
}

/* A 3D tilt toward the cursor, plus a soft light that follows it. */
function tilt(e: React.PointerEvent<HTMLButtonElement>) {
  const el = e.currentTarget;
  const b = el.getBoundingClientRect();
  const x = (e.clientX - b.left) / b.width;
  const y = (e.clientY - b.top) / b.height;
  el.style.setProperty('--mx', `${x * 100}%`);
  el.style.setProperty('--my', `${y * 100}%`);
  el.style.transform = `translateY(-6px) rotateX(${(0.5 - y) * 14}deg) rotateY(${(x - 0.5) * 16}deg)`;
}
