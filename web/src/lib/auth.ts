import { hash, verify } from '@node-rs/argon2';
import { getIronSession, type IronSession } from 'iron-session';
import { cookies } from 'next/headers';

import { prisma } from './db';

/* Authentication.
 *
 * Username and password, held in this project's own Postgres. No third-party
 * sign-in, by decision.
 *
 * Passwords are HASHED with argon2id, not encoded. The distinction matters and
 * is the reason this file exists rather than a two-line helper:
 *
 *   - Encoding (base64) is reversible. Anyone who reads the table reads every
 *     password, including whatever the user reuses elsewhere.
 *   - A fast hash (SHA-256, MD5) is not reversible but is trivially
 *     brute-forced -- a GPU does billions of those per second, and real
 *     passwords do not survive that.
 *   - argon2id is deliberately slow and memory-hard, so an attacker with the
 *     table still has to spend real time and real RAM per guess.
 *
 * The parameters below are the OWASP baseline: 19 MiB of memory, two passes.
 * They are stored inside the hash string, so raising them later does not
 * invalidate existing hashes -- argon2 reads each hash's own parameters when
 * verifying.
 */

const ARGON: Parameters<typeof hash>[1] = {
  memoryCost: 19456, // KiB
  timeCost: 2,
  outputLen: 32,
  parallelism: 1,
};

export function hashPassword(password: string): Promise<string> {
  return hash(password, ARGON);
}

export function verifyPassword(digest: string, password: string): Promise<boolean> {
  return verify(digest, password, ARGON);
}

/* -------------------------------------------------------------- sessions */

export type SessionData = {
  userId?: string;
  username?: string;
};

const SESSION_PASSWORD = process.env.SESSION_SECRET ?? '';

if (SESSION_PASSWORD.length < 32 && process.env.NODE_ENV === 'production') {
  throw new Error('SESSION_SECRET must be at least 32 characters in production');
}

export const sessionOptions = {
  password: SESSION_PASSWORD.padEnd(32, '0'),
  cookieName: 'btb_session',
  cookieOptions: {
    // httpOnly keeps the cookie out of reach of any script on the page, so an
    // XSS bug cannot walk off with a live session.
    httpOnly: true,
    // Secure is required in production and impossible in local http dev.
    secure: process.env.NODE_ENV === 'production',
    // 'lax' still sends the cookie on top-level navigation but not on
    // cross-site POSTs, which is the CSRF case that matters here.
    sameSite: 'lax' as const,
    path: '/',
    maxAge: 60 * 60 * 24 * 7,
  },
};

export async function getSession(): Promise<IronSession<SessionData>> {
  return getIronSession<SessionData>(await cookies(), sessionOptions);
}

export async function currentUser() {
  const session = await getSession();
  if (!session.userId) return null;
  return prisma.users.findUnique({
    where: { id: session.userId },
    select: { id: true, username: true },
  });
}

/* ------------------------------------------------------- login throttling */

/* In-memory attempt counter.
 *
 * Adequate for a single server and honest about its limits: it resets on
 * restart and does not span instances. The moment there is more than one app
 * container this moves to a table or to Redis -- noted in PLAN.md rather than
 * pretended away.
 */
const attempts = new Map<string, { n: number; until: number }>();
const MAX_ATTEMPTS = 8;
const LOCKOUT_MS = 10 * 60 * 1000;

export function throttleCheck(key: string): { blocked: boolean; retryIn?: number } {
  const rec = attempts.get(key);
  if (!rec) return { blocked: false };
  if (Date.now() > rec.until) {
    attempts.delete(key);
    return { blocked: false };
  }
  if (rec.n >= MAX_ATTEMPTS) {
    return { blocked: true, retryIn: Math.ceil((rec.until - Date.now()) / 1000) };
  }
  return { blocked: false };
}

export function throttleFail(key: string): void {
  const rec = attempts.get(key) ?? { n: 0, until: Date.now() + LOCKOUT_MS };
  rec.n += 1;
  rec.until = Date.now() + LOCKOUT_MS;
  attempts.set(key, rec);
}

export function throttleReset(key: string): void {
  attempts.delete(key);
}
