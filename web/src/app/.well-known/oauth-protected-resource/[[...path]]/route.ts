import { CORS, json, protectedResourceMetadata, publicOrigin } from '../../../../lib/oauth';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* RFC 9728 protected resource metadata for /api/mcp, at the bare path and at
 * the path-suffixed one (/.well-known/oauth-protected-resource/api/mcp) that
 * the MCP endpoint's 401 challenge points to. */
export function GET(req: Request) {
  return json(protectedResourceMetadata(publicOrigin(req)));
}

export function OPTIONS() {
  return new Response(null, { status: 204, headers: CORS });
}
