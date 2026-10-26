import { NextResponse } from 'next/server';

import { currentUser } from '../../../../lib/auth';
import { MAX_SOURCE_CHARS } from '../../../../lib/pine';
import { CheckUnavailable, checkPine } from '../../../../lib/pineCheck';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* POST { source } -> the script checked: errors and warnings with line numbers,
 * its inputs, the grid and N, its costs. Nothing runs and nothing is stored;
 * the Strategies tab calls this as the user types. */
export async function POST(req: Request) {
  const user = await currentUser();
  if (!user) return NextResponse.json({ error: 'Unauthorised' }, { status: 401 });
  let source: unknown;
  try {
    source = (await req.json())?.source;
  } catch {
    return NextResponse.json({ error: 'Expected a JSON body' }, { status: 400 });
  }
  if (typeof source !== 'string' || !source.trim()) {
    return NextResponse.json({ error: 'source is required' }, { status: 400 });
  }
  if (source.length > MAX_SOURCE_CHARS) {
    return NextResponse.json({ error: 'The script is too long' }, { status: 413 });
  }
  try {
    return NextResponse.json(await checkPine(source, user.id));
  } catch (e) {
    if (e instanceof CheckUnavailable) return NextResponse.json({ error: e.message }, { status: 503 });
    throw e;
  }
}
