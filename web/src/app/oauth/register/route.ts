import { randomUUID } from 'node:crypto';

import { prisma } from '../../../lib/db';
import { CORS, allowedRedirect, json, oauthError } from '../../../lib/oauth';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* Dynamic client registration (RFC 7591). Public clients only: no secret is
 * issued, and PKCE binds each authorization to the client that started it.
 * Registering grants nothing by itself -- every authorization still needs a
 * signed-in user to approve it. */
const MAX_URIS = 10;

export async function POST(req: Request) {
  let body: any;
  try {
    body = await req.json();
  } catch {
    return oauthError('invalid_client_metadata', 'Expected a JSON body');
  }
  const uris = body?.redirect_uris;
  if (!Array.isArray(uris) || !uris.length || uris.length > MAX_URIS
      || !uris.every((u) => typeof u === 'string' && u.length < 2000 && allowedRedirect(u))) {
    return oauthError('invalid_redirect_uri',
      'redirect_uris must be https URLs, or http URLs on localhost');
  }
  const name = typeof body.client_name === 'string' ? body.client_name.slice(0, 120) : null;
  const clientId = `btb_client_${randomUUID()}`;
  await prisma.oauth_clients.create({ data: { client_id: clientId, name, redirect_uris: uris } });
  return json({
    client_id: clientId,
    client_id_issued_at: Math.floor(Date.now() / 1000),
    client_name: name ?? undefined,
    redirect_uris: uris,
    token_endpoint_auth_method: 'none',
    grant_types: ['authorization_code', 'refresh_token'],
    response_types: ['code'],
  }, 201);
}

export function OPTIONS() {
  return new Response(null, { status: 204, headers: CORS });
}
