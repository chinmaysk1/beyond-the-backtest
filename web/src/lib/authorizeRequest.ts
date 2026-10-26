import { prisma } from './db';
import { normaliseScope } from './oauth';

/* An authorization request (the query of /oauth/authorize), checked against
 * the registered client. Shared by the consent page and the decision handler,
 * so what the user approved is exactly what gets issued.
 *
 * Until the client and redirect URI are known good, nothing redirects: an
 * error is shown on this site instead, so the endpoint cannot be used to
 * bounce a user to an arbitrary URL. */
export type AuthRequest = {
  clientId: string; clientName: string | null; redirectUri: string; state: string | null;
  codeChallenge: string; scope: string;
};

export type Parsed = { ok: true; req: AuthRequest } | { ok: false; error: string; redirect?: string };

export async function parseAuthorize(q: URLSearchParams): Promise<Parsed> {
  const clientId = q.get('client_id') ?? '';
  const redirectUri = q.get('redirect_uri') ?? '';
  const client = clientId ? await prisma.oauth_clients.findUnique({ where: { client_id: clientId } }) : null;
  if (!client) return { ok: false, error: 'This app is not registered with Beyond the Backtest.' };
  if (!client.redirect_uris.includes(redirectUri)) {
    return { ok: false, error: 'The app asked to return to an address it did not register.' };
  }
  // From here the redirect URI is the client's own, so errors go back to it.
  const state = q.get('state');
  const back = (error: string, description: string) => {
    const u = new URL(redirectUri);
    u.searchParams.set('error', error);
    u.searchParams.set('error_description', description);
    if (state) u.searchParams.set('state', state);
    return u.href;
  };
  if (q.get('response_type') !== 'code') {
    return { ok: false, error: 'Unsupported response type.', redirect: back('unsupported_response_type', 'Use response_type=code') };
  }
  const challenge = q.get('code_challenge') ?? '';
  if (q.get('code_challenge_method') !== 'S256' || !/^[A-Za-z0-9_-]{43,128}$/.test(challenge)) {
    return { ok: false, error: 'PKCE is required.', redirect: back('invalid_request', 'PKCE with S256 is required') };
  }
  const scope = normaliseScope(q.get('scope'));
  if (!scope) return { ok: false, error: 'Unknown scope.', redirect: back('invalid_scope', 'Unknown scope') };
  return { ok: true, req: { clientId, clientName: client.name, redirectUri, state, codeChallenge: challenge, scope } };
}
