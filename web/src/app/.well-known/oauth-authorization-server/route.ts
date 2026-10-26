import { CORS, authServerMetadata, json, publicOrigin } from '../../../lib/oauth';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* RFC 8414 authorization server metadata: where an MCP client registers,
 * sends the user to sign in, and exchanges the code. */
export function GET(req: Request) {
  return json(authServerMetadata(publicOrigin(req)));
}

export function OPTIONS() {
  return new Response(null, { status: 204, headers: CORS });
}
