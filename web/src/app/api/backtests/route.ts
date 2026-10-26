import { NextResponse } from 'next/server';

import { currentUser } from '../../../lib/auth';
import { queueTest } from '../../../lib/queueTest';
import { listTests } from '../../../lib/tests';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* Tests: the Results tab's list (GET), and queueing a backtest -- with, by
 * default, the sweep that finds and scores its best settings (POST).
 *
 * The app never runs the engine. It inserts `jobs` rows; the Python worker
 * claims them. Everything checkable here is checked here, so a bad request
 * fails fast with a message instead of occupying the queue -- but the worker
 * re-checks all of it, because it is the side spending the CPU.
 *
 * Both halves live in lib/ (tests.ts, queueTest.ts) because the MCP server
 * offers the same two things to a user's own AI, and the two must not drift.
 */

const bad = (error: string, status = 400) => NextResponse.json({ error }, { status });

export async function GET() {
  const user = await currentUser();
  if (!user) return bad('Unauthorised', 401);
  return NextResponse.json({ tests: await listTests(user.id) });
}

export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return bad('Unauthorised', 401);

  let body: any;
  try {
    body = await req.json();
  } catch {
    return bad('Expected a JSON body');
  }
  const out = await queueTest(user.id, body);
  if (!out.ok) return bad(out.error, out.status);
  return NextResponse.json({ id: out.id, sweepId: out.sweepId }, { status: 202 });
}
