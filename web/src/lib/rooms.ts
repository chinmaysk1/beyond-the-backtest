/* The four sections of the app. The landing shows them as cards; inside the
 * app they are the nav tabs, in the same order -- the transition flies each
 * card into its tab, so the order is load-bearing. */

export type RoomId = 'data' | 'strategies' | 'results' | 'paper';

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
  { id: 'results', title: 'Results', live: true,
    desc: 'Every test, its verdict, and every setting it tried.',
    icon: 'M5 12l4 4L19 6' },
  { id: 'paper', title: 'Paper trading', live: false,
    desc: 'Live prices, pretend money, before real money.',
    icon: 'M3 12h4l3-8 4 16 3-8h4' },
];
