import { redirect } from 'next/navigation';

import Brand from '../../../components/Brand';
import { currentUser } from '../../../lib/auth';
import { parseAuthorize } from '../../../lib/authorizeRequest';

export const dynamic = 'force-dynamic';

/* The one screen a connector shows the user: who is asking, for what, and
 * Allow or Deny. Signing in comes first if needed, then back here. */
export default async function Authorize({ searchParams }: { searchParams: Promise<Record<string, string | string[] | undefined>> }) {
  const raw = await searchParams;
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(raw)) if (typeof v === 'string') q.set(k, v);

  const parsed = await parseAuthorize(q);
  if (!parsed.ok) {
    if (parsed.redirect) redirect(parsed.redirect);
    return <Shell><h1>Can’t connect</h1><p className="login-error">{parsed.error}</p></Shell>;
  }
  const user = await currentUser();
  if (!user) redirect(`/login?next=${encodeURIComponent(`/oauth/authorize?${q.toString()}`)}`);

  const r = parsed.req;
  const app = r.clientName ?? 'An AI app';
  const can = [
    r.scope.includes('tests:read') && 'read your tests and their scores',
    r.scope.includes('tests:run') && 'check Pine scripts and run new tests on your account',
  ].filter(Boolean) as string[];
  return (
    <Shell>
      <h1>Connect {app}?</h1>
      <p className="oauth-lede">
        <b>{app}</b> wants to act as <b>{user.username}</b> on Beyond the Backtest. It will be able to:
      </p>
      <ul className="oauth-can">{can.map((c) => <li key={c}>{c}</li>)}</ul>
      <p className="oauth-note">
        It can’t see anyone else’s tests or change your password. You can disconnect it from
        Strategies → Connect your AI at any time.
      </p>
      <form method="post" action="/oauth/authorize/decision" className="oauth-actions">
        {Array.from(q.entries()).map(([k, v]) => <input key={k} type="hidden" name={k} value={v} />)}
        <button className="btn-primary" type="submit" name="decision" value="allow">Allow</button>
        <button className="btn-ghost" type="submit" name="decision" value="deny">Deny</button>
      </form>
      <p className="oauth-note">Returns to <span className="mono">{new URL(r.redirectUri).host}</span></p>
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="login">
      <div className="login-card oauth-card">
        <Brand />
        {children}
      </div>
    </div>
  );
}
