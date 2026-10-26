import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { prisma } from '../../../lib/db';
import { createPersonalToken } from '../../../lib/tokens';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* Personal access tokens, for Claude Code and anything else that takes a
 * bearer token: list (GET), create (POST, shown once), revoke (DELETE ?id).
 * GET also lists the apps connected by OAuth, and DELETE ?client=ID
 * disconnects one, revoking every token it holds. Signed-in users only; a
 * token cannot mint more tokens. */
const MAX_TOKENS = 10;

export async function GET() {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  const rows = await prisma.api_tokens.findMany({
    where: { user_id: user.id, kind: 'personal', revoked_at: null },
    orderBy: { created_at: 'desc' },
    select: { id: true, name: true, created_at: true, last_used_at: true },
  });
  const grants = await prisma.api_tokens.findMany({
    where: { user_id: user.id, kind: 'refresh', revoked_at: null, expires_at: { gt: new Date() } },
    orderBy: { created_at: 'desc' },
    select: { client_id: true, created_at: true, oauth_clients: { select: { name: true } } },
  });
  const apps = new Map<string, { clientId: string; name: string | null; since: string }>();
  for (const g of grants) {
    if (g.client_id && !apps.has(g.client_id)) {
      apps.set(g.client_id, { clientId: g.client_id, name: g.oauth_clients?.name ?? null,
                              since: g.created_at.toISOString() });
    }
  }
  return NextResponse.json({
    tokens: rows.map((r) => ({
      id: r.id, name: r.name, createdAt: r.created_at.toISOString(),
      lastUsedAt: r.last_used_at?.toISOString() ?? null,
    })),
    apps: [...apps.values()],
  });
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  const body = await req.json().catch(() => ({}));
  const live = await prisma.api_tokens.count({ where: { user_id: user.id, kind: 'personal', revoked_at: null } });
  if (live >= MAX_TOKENS) {
    return NextResponse.json({ error: `You have ${live} tokens; revoke one first` }, { status: 429 });
  }
  const name = typeof body?.name === 'string' ? body.name.slice(0, 60) : null;
  return NextResponse.json({ token: await createPersonalToken(user.id, name) }, { status: 201 });
}

export async function DELETE(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  const q = new URL(req.url).searchParams;
  const client = q.get('client');
  if (client) {
    const n = await prisma.api_tokens.updateMany({
      where: { user_id: user.id, client_id: client, revoked_at: null }, data: { revoked_at: new Date() },
    });
    return n.count ? NextResponse.json({ ok: true }) : NextResponse.json({ error: 'Not found' }, { status: 404 });
  }
  const id = q.get('id') ?? '';
  const n = await prisma.api_tokens.updateMany({
    where: { id, user_id: user.id, kind: 'personal', revoked_at: null }, data: { revoked_at: new Date() },
  }).catch(() => ({ count: 0 }));
  return n.count ? NextResponse.json({ ok: true }) : NextResponse.json({ error: 'Not found' }, { status: 404 });
}
