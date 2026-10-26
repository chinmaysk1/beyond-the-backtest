import { createHash } from 'node:crypto';

import { DEFAULT_SCOPE, SCOPES } from './tokens';

/* The site as an OAuth 2.1 authorization server for its own MCP endpoint.
 *
 * Just enough for MCP clients (Claude, ChatGPT, Claude Code) to attach without
 * a manual step: dynamic client registration (RFC 7591), the authorization
 * code grant with PKCE S256 required, refresh tokens, and the two discovery
 * documents (RFC 8414, RFC 9728). Public clients only -- an MCP client cannot
 * keep a secret -- so PKCE is what binds a code to the client that asked.
 */

/* The site's public origin. Behind Caddy the app sees its container address,
 * so the configured URL wins; locally the request's own origin is right. */
export function publicOrigin(req: Request): string {
  const env = process.env.BTB_PUBLIC_URL?.replace(/\/+$/, '');
  if (env) return env;
  const u = new URL(req.url);
  const proto = req.headers.get('x-forwarded-proto') ?? u.protocol.replace(':', '');
  const host = req.headers.get('x-forwarded-host') ?? req.headers.get('host') ?? u.host;
  return `${proto}://${host}`;
}

export function mcpUrl(origin: string): URL {
  return new URL('/api/mcp', origin);
}

export function authServerMetadata(origin: string) {
  return {
    issuer: origin,
    authorization_endpoint: `${origin}/oauth/authorize`,
    token_endpoint: `${origin}/oauth/token`,
    registration_endpoint: `${origin}/oauth/register`,
    response_types_supported: ['code'],
    grant_types_supported: ['authorization_code', 'refresh_token'],
    code_challenge_methods_supported: ['S256'],
    token_endpoint_auth_methods_supported: ['none'],
    scopes_supported: [...SCOPES],
  };
}

export function protectedResourceMetadata(origin: string) {
  return {
    resource: mcpUrl(origin).href,
    authorization_servers: [origin],
    scopes_supported: [...SCOPES],
    bearer_methods_supported: ['header'],
    resource_name: 'Beyond the Backtest',
  };
}

export const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type, Authorization, MCP-Protocol-Version, Mcp-Session-Id',
  'Access-Control-Expose-Headers': 'WWW-Authenticate, Mcp-Session-Id',
};

export function json(body: unknown, status = 200, extra: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', ...CORS, ...extra },
  });
}

export function oauthError(error: string, description: string, status = 400) {
  return json({ error, error_description: description }, status);
}

/* Redirect URIs a client may register: https anywhere, or http only back to
 * this machine (Claude Code and other local clients listen on localhost). */
export function allowedRedirect(uri: string): boolean {
  try {
    const u = new URL(uri);
    if (u.hash) return false;
    if (u.protocol === 'https:') return true;
    return u.protocol === 'http:' && ['localhost', '127.0.0.1', '[::1]'].includes(u.hostname);
  } catch {
    return false;
  }
}

export function normaliseScope(scope: string | null | undefined): string | null {
  if (!scope) return DEFAULT_SCOPE;
  const asked = scope.split(/\s+/).filter(Boolean);
  if (!asked.every((s) => (SCOPES as readonly string[]).includes(s))) return null;
  return SCOPES.filter((s) => asked.includes(s)).join(' ');
}

export function pkceS256(verifier: string): string {
  return createHash('sha256').update(verifier, 'ascii').digest('base64url');
}
