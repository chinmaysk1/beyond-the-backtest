import { createHash } from 'node:crypto';

import { prisma } from './db';
import type { PineCheck } from './pine';

/* Checking Pine from the web side.
 *
 * The transpiler is Python (PineForge's pineforge_codegen), so the app cannot
 * check a script itself. It queues a `check` job and waits for the checker --
 * a second worker process that claims nothing else, so a long sweep never
 * makes the Strategies tab wait for a syntax check -- typically well under a
 * second.
 *
 * Results are cached in memory by source hash for a few minutes: the same
 * script checked as the user types, then again when they press Run, is one
 * job. The expiry is what lets a redeployed checker's answers show up without
 * restarting the app. The engine re-checks every script it runs anyway; this
 * only decides what to show and whether to queue.
 */

const WAIT_MS = 15_000;
const POLL_MS = 120;
const CACHE_MAX = 300;
const CACHE_TTL_MS = 10 * 60 * 1000;
const cache = new Map<string, { at: number; check: PineCheck }>();

export class CheckUnavailable extends Error {}

export function sourceKey(source: string): string {
  // Same normalisation as btb/pine/check.py:source_sha.
  const norm = source.replace(/\r\n/g, '\n').split('\n').map((l) => l.trimEnd()).join('\n').trim();
  return createHash('sha256').update(norm, 'utf8').digest('hex');
}

export async function checkPine(source: string, userId: string | null): Promise<PineCheck> {
  const key = sourceKey(source);
  const hit = cache.get(key);
  if (hit && Date.now() - hit.at < CACHE_TTL_MS) return hit.check;

  const job = await prisma.jobs.create({
    data: { user_id: userId, kind: 'check', payload: { source }, priority: 10 },
    select: { id: true },
  });
  const until = Date.now() + WAIT_MS;
  while (Date.now() < until) {
    await new Promise((r) => setTimeout(r, POLL_MS));
    const j = await prisma.jobs.findUnique({
      where: { id: job.id }, select: { status: true, result: true, error: true },
    });
    if (!j) break;
    if (j.status === 'done' && j.result) {
      const out = j.result as unknown as PineCheck;
      if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string);
      cache.set(key, { at: Date.now(), check: out });
      // Check jobs are answers, not history: nothing reads them once read here.
      await prisma.jobs.delete({ where: { id: job.id } }).catch(() => {});
      return out;
    }
    if (j.status === 'failed') {
      await prisma.jobs.delete({ where: { id: job.id } }).catch(() => {});
      throw new CheckUnavailable(j.error ?? 'The checker failed');
    }
  }
  await prisma.jobs.update({ where: { id: job.id }, data: { status: 'cancelled' } }).catch(() => {});
  throw new CheckUnavailable('The checker did not answer in time. Try again in a moment.');
}
