import type { Metadata } from 'next';
import { Poppins } from 'next/font/google';

import './globals.css';

/* A geometric sans with a SINGLE-STOREY 'a' and circular bowls, matching the
 * reference. Inter was the wrong call: its 'a' is double-storey, which is the
 * most visible letterform on a page of short labels and the reason the type
 * never looked right next to the reference.
 *
 * Self-hosted by next/font rather than pulled from Google's CDN at runtime --
 * one fewer third-party request per load, and no flash of fallback text. */
const display = Poppins({
  subsets: ['latin'],
  weight: ['400', '500', '600'],
  display: 'swap',
  variable: '--font-display',
});

export const metadata: Metadata = {
  title: 'Beyond the Backtest',
  description: 'Guided strategy discovery and validation',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={display.variable}>
      <body>{children}</body>
    </html>
  );
}
