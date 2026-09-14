import { NextResponse } from 'next/server';

import {
  getSession, throttleCheck, throttleFail, throttleReset, verifyPassword,
} from '../../../../lib/auth';
import { prisma } from '../../../../lib/db';

// argon2 is a native module and cannot run on the edge runtime.
export const runtime = 'nodejs';

export async function POST(req: Request) {
  const { username, password } = (await req.json()) as {
    username?: string; password?: string;
  };

  if (!username || !password) {
    return NextResponse.json({ error: 'Missing credentials' }, { status: 400 });
  }

  // Throttle per username. A public login endpoint on a 2-core box is a
  // password-spraying target, and argon2 is deliberately expensive -- which
  // makes unthrottled attempts a denial-of-service vector as well as a
  // credential one.
  const key = username.toLowerCase();
  const t = throttleCheck(key);
  if (t.blocked) {
    return NextResponse.json(
      { error: `Too many attempts. Try again in ${t.retryIn}s.` },
      { status: 429 },
    );
  }

  const user = await prisma.users.findUnique({ where: { username } });

  // One generic message for both "no such user" and "wrong password". Saying
  // which half was wrong hands an attacker a way to enumerate valid usernames.
  const deny = () => {
    throttleFail(key);
    return NextResponse.json({ error: 'Invalid username or password' }, { status: 401 });
  };

  if (!user?.password_hash) return deny();

  let ok = false;
  try {
    ok = await verifyPassword(user.password_hash, password);
  } catch {
    ok = false;          // a malformed stored hash is a failed login, not a 500
  }
  if (!ok) return deny();

  throttleReset(key);

  const session = await getSession();
  session.userId = user.id;
  session.username = user.username ?? undefined;
  await session.save();

  return NextResponse.json({ ok: true, username: user.username });
}
