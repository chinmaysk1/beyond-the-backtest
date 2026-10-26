import { redirect } from 'next/navigation';

import LoginForm from '../../components/LoginForm';
import { currentUser } from '../../lib/auth';

export const dynamic = 'force-dynamic';

/* `next` returns the user where they were going -- the OAuth consent screen,
 * when an AI app sent them here. Only a path on this site is accepted, so the
 * login page can never be used to send someone elsewhere. */
function safeNext(next: unknown): string {
  // A single leading slash, not "//" or "/\" (both of which browsers read as
  // another host).
  return typeof next === 'string' && /^\/(?![/\\])/.test(next) ? next : '/';
}

export default async function LoginPage({ searchParams }: { searchParams: Promise<{ next?: string }> }) {
  const next = safeNext((await searchParams).next);
  if (await currentUser()) redirect(next);
  return <LoginForm next={next} />;
}
