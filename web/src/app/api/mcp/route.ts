import {
  OAuthError, bearerAuthChallengeResponse, createMcpHandler, getOAuthProtectedResourceMetadataUrl,
  verifyBearerToken, type AuthInfo,
} from '@modelcontextprotocol/server';

import { buildServer } from '../../../lib/mcp';
import { CORS, mcpUrl, publicOrigin } from '../../../lib/oauth';
import { DEFAULT_SCOPE, verify } from '../../../lib/tokens';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';
// get_test can wait up to 50 s; the proxy in front (Caddy) allows longer.
export const maxDuration = 60;

/* The MCP endpoint (streamable HTTP). Every request needs a bearer token --
 * an OAuth access token from a connector, or a personal access token -- and
 * is served by a fresh server bound to that token's user (lib/mcp.ts).
 *
 * A missing or bad token gets a 401 whose WWW-Authenticate header points to
 * the protected-resource metadata, which is how a connector discovers where
 * to send the user to sign in. */

const handler = createMcpHandler((ctx) => {
  const userId = ctx.authInfo?.extra?.userId;
  const origin = String(ctx.authInfo?.extra?.origin ?? '');
  if (typeof userId !== 'string') throw new Error('unauthenticated request reached the MCP server');
  return buildServer(userId, origin);
}, { onerror: (e) => console.error('[mcp]', e.message) });

async function serve(req: Request): Promise<Response> {
  const origin = publicOrigin(req);
  const resourceMetadataUrl = getOAuthProtectedResourceMetadataUrl(mcpUrl(origin));
  let authInfo: AuthInfo;
  try {
    authInfo = await verifyBearerToken(req.headers.get('authorization'), {
      resourceMetadataUrl,
      requiredScopes: DEFAULT_SCOPE.split(' '),
      verifier: {
        async verifyAccessToken(token: string): Promise<AuthInfo> {
          const v = await verify(token, ['access', 'personal']);
          if (!v) throw new OAuthError('invalid_token', 'The token is invalid, expired or revoked');
          return {
            token, clientId: v.clientId ?? 'personal-token', scopes: v.scopes,
            // The SDK refuses a token without an expiry. A personal token has
            // none until revoked; each request is given a short horizon.
            expiresAt: v.expiresAt ?? Math.floor(Date.now() / 1000) + 3600,
            extra: { userId: v.userId, origin },
          };
        },
      },
    });
  } catch (e) {
    const res = bearerAuthChallengeResponse(e, { resourceMetadataUrl });
    return withCors(res);
  }
  return withCors(await handler.fetch(req, { authInfo }));
}

function withCors(res: Response): Response {
  const headers = new Headers(res.headers);
  for (const [k, v] of Object.entries(CORS)) headers.set(k, v);
  headers.set('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}

export const POST = serve;
export const GET = serve;
export const DELETE = serve;

export function OPTIONS() {
  return new Response(null, { status: 204, headers: { ...CORS, 'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS' } });
}
