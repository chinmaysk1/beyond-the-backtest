import type { Job } from './types';

/* Queueing and following worker jobs, shared by the Strategies tab (which
 * starts a test) and a test's page in Results (which re-runs one). */

export const POLL_MS = 600;

export async function submitJob(body: Record<string, unknown>): Promise<{ id: number; sweepId: number | null }> {
  const res = await fetch('/api/backtests', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const out = await res.json();
  if (!res.ok) throw new Error(out.error ?? 'Could not queue the job');
  return out;
}

/* Poll one job until it settles. `alive` is checked every round so an
 * unmounted view stops polling; a 401 throws 'signed out' for the caller to
 * redirect on. Network errors are retried, not surfaced. */
export async function pollJob(id: number, onUpdate: (j: Job) => void, alive: () => boolean): Promise<Job> {
  for (;;) {
    if (!alive()) throw new Error('gone');
    try {
      const res = await fetch(`/api/backtests/${id}`);
      if (res.status === 401) throw new Error('signed out');
      const job: Job = await res.json();
      onUpdate(job);
      if (job.status === 'done' || job.status === 'failed' || job.status === 'cancelled') return job;
    } catch (e) {
      if ((e as Error).message === 'signed out') throw e;
    }
    await new Promise((r) => setTimeout(r, POLL_MS));
  }
}
