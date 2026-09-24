import type { Metadata } from 'next';
import { Poppins, Sora, Unbounded } from 'next/font/google';

import './globals.css';
import './experience.css';

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

/* The landing wordmark: a thin, wide geometric face, set in spaced capitals. */
const wordmark = Sora({
  subsets: ['latin'],
  weight: ['200', '400'],
  display: 'swap',
  variable: '--font-wordmark',
});

/* Display numerals and headings on the Strategies results page. */
const headline = Unbounded({
  subsets: ['latin'],
  weight: ['300', '400'],
  display: 'swap',
  variable: '--font-headline',
});

export const metadata: Metadata = {
  title: 'Beyond the Backtest',
  description: 'Guided strategy discovery and validation',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${display.variable} ${wordmark.variable} ${headline.variable}`}>
      <body>{children}</body>
    </html>
  );
}
