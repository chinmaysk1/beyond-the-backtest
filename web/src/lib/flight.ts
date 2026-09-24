/* The landing <-> app transition: landing cards fly into the nav tabs, and the
 * wordmark flies into the nav brand (and back).
 *
 * FLIP with throwaway clones rather than animating the real elements: the
 * cards and tabs live in different parts of the tree with different styles,
 * so a clone that starts as one and ends as the other is the only way to make
 * them read as the same object. The Web Animations API runs it on the
 * compositor where it can, and every clone is removed when it lands.
 */

const EASE_OUT = 'cubic-bezier(.16,1,.3,1)';
const EASE_IO = 'cubic-bezier(.65,0,.35,1)';

// fill 'both', not 'forwards': every flight has a start delay (the stagger, or
// the pause before the way home), and 'forwards' applies nothing DURING the
// delay -- so the clones render unstyled for those frames, which showed as the
// big wordmark flashing on screen before the trip home began.

type Box = { x: number; y: number; w: number; h: number };
const box = (el: Element): Box => {
  const r = el.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height };
};
const frame = (b: Box, radius: string) => ({
  left: `${b.x}px`, top: `${b.y}px`, width: `${b.w}px`, height: `${b.h}px`, borderRadius: radius,
});

/** Cards -> tabs (or tabs -> cards with `back`). Resolves when all have landed. */
export function flyCards(cards: HTMLElement[], tabs: HTMLElement[], labels: string[],
                         active: number, back = false): Promise<void> {
  const from = (back ? tabs : cards).map(box);
  const to = (back ? cards : tabs).map(box);
  const runs = from.map((f, i) => {
    const el = document.createElement('div');
    el.className = 'flyer';
    const span = document.createElement('span');
    span.textContent = labels[i];
    el.appendChild(span);
    document.body.appendChild(el);
    const on = i === active;
    const card = { ...frame(back ? to[i] : f, '18px'), background: 'rgba(23,26,30,.7)', color: '#9aa1a9' };
    const tab = { ...frame(back ? f : to[i], '12px'),
      background: on ? '#1d2126' : '#171a1e', color: on ? '#e7e9ec' : '#9aa1a9' };
    const opts: KeyframeAnimationOptions = {
      duration: back ? 850 : 900, delay: (back ? 120 : 0) + i * 45, easing: EASE_OUT, fill: 'both',
    };
    const a = el.animate(back ? [tab, { ...card, opacity: 0.2 }] : [card, tab], opts);
    span.animate(back ? [{ fontSize: '12.5px' }, { fontSize: '15px' }]
                      : [{ fontSize: '15px' }, { fontSize: '12.5px' }], opts);
    return a.finished.then(() => el.remove());
  });
  return Promise.all(runs).then(() => undefined);
}

/** The big wordmark into the small brand (or back). */
export function flyTitle(title: HTMLElement, brand: HTMLElement, back = false): Promise<void> {
  const big = box(title), small = box(brand);
  const el = title.cloneNode(true) as HTMLElement;
  el.removeAttribute('id');
  el.classList.add('title-flyer');
  Object.assign(el.style, { left: `${big.x}px`, top: `${big.y}px`, width: `${big.w}px` });
  document.body.appendChild(el);
  const scale = (small.h / big.h) * 1.1;
  const away = { transform: `translate(${small.x - big.x}px, ${small.y - big.y - 2}px) scale(${scale})`, opacity: 0 };
  const home = { transform: 'none', opacity: 1 };
  // Gone by mid-flight: the room is rising underneath, and a large wordmark
  // sliding across its content reads as a collision, not a hand-off. The nav
  // brand fades in to meet it (experience.css).
  const mid = { opacity: 0, offset: back ? 0.5 : 0.45 };
  const a = el.animate(back ? [away, mid, home] : [home, mid, away], {
    duration: back ? 1000 : 950, delay: back ? 150 : 0, easing: back ? EASE_OUT : EASE_IO, fill: 'both',
  });
  return a.finished.then(() => el.remove());
}
