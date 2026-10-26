import { prisma } from '../../../lib/db';
import { CORS, json, oauthError, pkceS256 } from '../../../lib/oauth';
import { issueOAuthTokens, revoke, sha256, verify } from '../../../lib/tokens';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* The token endpoint: an authorization code (with its PKCE verifier) or a
 * refresh token, for an access token and a new refresh token. Refresh tokens
 * rotate -- each is good once -- so a stolen one stops working the moment the
 * real client uses its own. */
export async function POST(req: Request) {
  const form = await readForm(req);
  if (!form) return oauthError('invalid_request', 'Expected a form or JSON body');
  const grant = form.get('grant_type');
  const clientId = form.get('client_id') ?? '';

  if (grant === 'authorization_code') {
    const code = form.get('code') ?? '';
    const verifier = form.get('code_verifier') ?? '';
    const redirectUri = form.get('redirect_uri') ?? '';
    if (!code || !verifier) return oauthError('invalid_request', 'code and code_verifier are required');
    const row = await prisma.oauth_codes.findUnique({ where: { code_hash: sha256(code) } });
    // One use only. A code presented twice is treated as stolen.
    if (!row || row.used_at || row.expires_at.getTime() <= Date.now()) {
      return oauthError('invalid_grant', 'The code is invalid, expired or already used');
    }
    if (row.client_id !== clientId || row.redirect_uri !== redirectUri) {
      return oauthError('invalid_grant', 'The code was issued to another client or redirect URI');
    }
    if (pkceS256(verifier) !== row.code_challenge) {
      return oauthError('invalid_grant', 'The PKCE verifier does not match');
    }
    const claimed = await prisma.oauth_codes.updateMany({
      where: { code_hash: row.code_hash, used_at: null }, data: { used_at: new Date() },
    });
    if (claimed.count !== 1) return oauthError('invalid_grant', 'The code was already used');
    return json(await issueOAuthTokens(row.user_id, row.client_id, row.scope));
  }

  if (grant === 'refresh_token') {
    const token = form.get('refresh_token') ?? '';
    const v = await verify(token, ['refresh']);
    if (!v || v.clientId !== clientId) return oauthError('invalid_grant', 'The refresh token is invalid');
    await revoke(v.tokenId);
    return json(await issueOAuthTokens(v.userId, clientId, v.scopes.join(' ')));
  }

  return oauthError('unsupported_grant_type', 'Use authorization_code or refresh_token');
}

export function OPTIONS() {
  return new Response(null, { status: 204, headers: CORS });
}

async function readForm(req: Request): Promise<URLSearchParams | null> {
  const type = req.headers.get('content-type') ?? '';
  try {
    if (type.includes('application/json')) {
      const body = await req.json();
      return new URLSearchParams(Object.entries(body ?? {}).map(([k, v]) => [k, String(v)]));
    }
    return new URLSearchParams(await req.text());
  } catch {
    return null;
  }
}
