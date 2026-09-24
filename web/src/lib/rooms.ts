/* The five sections of the app. The landing shows them as cards; inside the
 * app they are the nav tabs, in the same order -- the transition flies each
 * card into its tab, so the order is load-bearing. */

export type RoomId = 'data' | 'strategies' | 'sweeps' | 'results' | 'paper';

export type Room = {
  id: RoomId;
  title: string;
  desc: string;
  icon: string;      // SVG path data, 24x24 viewBox, stroked
  live: boolean;
};

export const ROOMS: Room[] = [
  { id: 'data', title: 'Data', live: true,
    desc: '24 markets, audited on every fetch, updated as bars close.',
    icon: 'M4 18V9M9 18V5M14 18v-6M19 18V8' },
  { id: 'strategies', title: 'Strategies', live: true,
    desc: 'Pick an idea, or write your own, and test it properly.',
    icon: 'M4 17l5-5 4 4 7-8M15 8h5v5' },
  { id: 'sweeps', title: 'Sweeps', live: false,
    desc: 'Every setting it tried, and whether the best was luck.',
    icon: 'M6 6h.01M18 6h.01M12 12h.01M6 18h.01M18 18h.01' },
  { id: 'results', title: 'Results', live: false,
    desc: 'Robust, Weak or Overfit. One word per strategy.',
    icon: 'M5 12l4 4L19 6' },
  { id: 'paper', title: 'Paper trading', live: false,
    desc: 'Live prices, pretend money, before real money.',
    icon: 'M3 12h4l3-8 4 16 3-8h4' },
];
