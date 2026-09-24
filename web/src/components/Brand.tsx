import type { Ref } from 'react';

/* The mark and name. In the app's nav it is the way home -- a home icon, and
 * clicking it plays the landing transition in reverse. `wordRef` is where the
 * landing wordmark lands. The login page keeps the solid mark. */
export default function Brand({ onClick, wordRef }: { onClick?: () => void; wordRef?: Ref<HTMLElement> }) {
  if (!onClick) {
    return (
      <div className="brand">
        <div className="mark"><span /></div>
        <div><b>Beyond the Backtest</b></div>
      </div>
    );
  }
  return (
    <button type="button" className="brand brand-btn" onClick={onClick} title="Home" aria-label="Home">
      <svg className="home-ic" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3.5 10.5 12 3.8l8.5 6.7" />
        <path d="M5.8 9v10.2h12.4V9" />
        <path d="M10 19.2v-5.4h4v5.4" />
      </svg>
      <b ref={wordRef as Ref<HTMLElement>}>Beyond the Backtest</b>
    </button>
  );
}
