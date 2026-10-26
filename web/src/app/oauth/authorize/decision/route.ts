import { prisma } from '../../../../lib/db';
import { currentUser } from '../../../../lib/auth';
import { parseAuthorize } from '../../../../lib/authorizeRequest';
import { randomToken, sha256 } from '../../../../lib/tokens';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* The consent form's POST. The session cookie is SameSite=Lax, so another
 * site cannot submit this form as the user; the request is re-checked from
 * scratch rather than trusted from the hidden fields. Allow issues a
 * single-use code, valid for ten minutes, bound to the PKCE challenge. */
const CODE_TTL_MS = 10 * 60 * 1000;

export async function POST(req: Request) {
  const form = new URLSearchParams(await req.text());
  const decision = form.get('decision');
  form.delete('decision');
  const parsed = await parseAuthorize(form);
  if (!parsed.ok) {
    return parsed.redirect ? Response.redirect(parsed.redirect, 303)
      : new Response(parsed.error, { status: 400 });
  }
  const r = parsed.req;
  const back = new URL(r.redirectUri);
  if (r.state) back.searchParams.set('state', r.state);

  const user = await currentUser();
  if (!user) return new Response('Sign in first', { status: 401 });
  if (decision !== 'allow') {
    back.searchParams.set('error', 'access_denied');
    return Response.redirect(back.href, 303);
  }
  const code = randomToken('btb_code_');
  await prisma.oauth_codes.create({
    data: { code_hash: sha256(code), client_id: r.clientId, user_id: user.id, redirect_uri: r.redirectUri,
            code_challenge: r.codeChallenge, scope: r.scope, expires_at: new Date(Date.now() + CODE_TTL_MS) },
  });
  back.searchParams.set('code', code);
  return Response.redirect(back.href, 303);
}
