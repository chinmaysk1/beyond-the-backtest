import { createHash, randomBytes, randomUUID } from 'node:crypto';

import { prisma } from './db';

/* Bearer tokens: what a user's own AI holds to act on their account.
 *
 * Three kinds in one table (api_tokens): OAuth access tokens (an hour),
 * OAuth refresh tokens (30 days, rotated on every use), and personal access
 * tokens a user creates for Claude Code (no expiry until revoked).
 *
 * Only the sha256 of a token is stored. A token is 32 random bytes, so a
 * fast hash is the right one here -- unlike a password there is nothing to
 * brute-force -- and it lets a lookup be one indexed equality.
 */

export const SCOPES = ['tests:read', 'tests:run'] as const;
export const DEFAULT_SCOPE = SCOPES.join(' ');
const ACCESS_TTL_S = 60 * 60;
const REFRESH_TTL_S = 60 * 60 * 24 * 30;
// last_used_at is for the token list ("last used 3 days ago"), not an audit
// log: writing it on every MCP call would be a write per tool call.
const TOUCH_EVERY_MS = 5 * 60 * 1000;

export type Kind = 'access' | 'refresh' | 'personal';
export type Verified = { userId: string; scopes: string[]; kind: Kind; clientId: string | null;
                         expiresAt: number | null; tokenId: string };

export function sha256(s: string): string {
  return createHash('sha256').update(s, 'utf8').digest('hex');
}

export function randomToken(prefix: string): string {
  return prefix + randomBytes(32).toString('base64url');
}

const PREFIX: Record<Kind, string> = { access: 'btb_at_', refresh: 'btb_rt_', personal: 'btb_pat_' };

async function store(userId: string, kind: Kind, scope: string, opts: { clientId?: string | null; name?: string | null; ttlS?: number }) {
  const token = randomToken(PREFIX[kind]);
  await prisma.api_tokens.create({
    data: {
      id: randomUUID(), user_id: userId, token_hash: sha256(token), kind, scope,
      client_id: opts.clientId ?? null, name: opts.name ?? null,
      expires_at: opts.ttlS ? new Date(Date.now() + opts.ttlS * 1000) : null,
    },
  });
  return token;
}

export function createPersonalToken(userId: string, name: string | null) {
  return store(userId, 'personal', DEFAULT_SCOPE, { name });
}

export async function issueOAuthTokens(userId: string, clientId: string, scope: string) {
  const access = await store(userId, 'access', scope, { clientId, ttlS: ACCESS_TTL_S });
  const refresh = await store(userId, 'refresh', scope, { clientId, ttlS: REFRESH_TTL_S });
  return { access_token: access, token_type: 'Bearer', expires_in: ACCESS_TTL_S,
           refresh_token: refresh, scope };
}

/* A presented token -> who it acts for, or null. Expired and revoked tokens
 * are refused; `kinds` limits which kinds are accepted where (a refresh token
 * is not a bearer credential for the MCP endpoint). */
export async function verify(token: string, kinds: Kind[]): Promise<Verified | null> {
  if (!/^btb_(at|rt|pat)_[A-Za-z0-9_-]{40,}$/.test(token)) return null;
  const row = await prisma.api_tokens.findUnique({ where: { token_hash: sha256(token) } });
  if (!row || row.revoked_at || !kinds.includes(row.kind as Kind)) return null;
  if (row.expires_at && row.expires_at.getTime() <= Date.now()) return null;
  if (!row.last_used_at || Date.now() - row.last_used_at.getTime() > TOUCH_EVERY_MS) {
    await prisma.api_tokens.update({ where: { id: row.id }, data: { last_used_at: new Date() } }).catch(() => {});
  }
  return { userId: row.user_id, scopes: row.scope.split(' ').filter(Boolean), kind: row.kind as Kind,
           clientId: row.client_id, expiresAt: row.expires_at ? Math.floor(row.expires_at.getTime() / 1000) : null,
           tokenId: row.id };
}

export async function revoke(tokenId: string) {
  await prisma.api_tokens.update({ where: { id: tokenId }, data: { revoked_at: new Date() } });
}
