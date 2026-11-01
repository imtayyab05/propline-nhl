// Shared GitHub workflow dispatch.
//
// Used by both the dashboard's "Update Now" button (functions/trigger.mjs) and the
// scheduled functions. One implementation means the scheduled path is the same code
// that has been proven to work by hand, rather than a second, untested one.

const GITHUB_API = 'https://api.github.com';
const WORKFLOW = 'daily.yml';

/**
 * Fire the daily-slate workflow.
 * @returns {{ok: boolean, status: number, detail?: string, hint?: string}}
 */
export async function dispatchWorkflow(inputs = {}) {
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;           // e.g. "imtayyab05/propline-nhl"
  const branch = process.env.GITHUB_BRANCH || 'main';

  if (!token || !repo) {
    return { ok: false, status: 500,
             detail: 'GITHUB_TOKEN / GITHUB_REPO are not set in the site environment' };
  }

  const r = await fetch(
    `${GITHUB_API}/repos/${repo}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ ref: branch, inputs }),
    }
  );

  if (r.status === 204) return { ok: true, status: 204 };

  const detail = (await r.text()).slice(0, 300);
  // A 404 here nearly always means the token lacks Actions write permission, rather
  // than the workflow genuinely being missing.
  const hint = r.status === 404
    ? 'Workflow not found, or the token lacks "Actions: write" on this repository.'
    : undefined;
  return { ok: false, status: r.status, detail, hint };
}

/**
 * Is a slate run waiting to start? GitHub keeps at most ONE waiting run per concurrency
 * group, and a new dispatch cancels it (only runs already in progress are safe). Seen
 * 1 Nov 2026: an override saved at 16:30 displaced the 16:30 wave that was still queued.
 * A run that is waiting will read the latest overrides when it starts anyway, so callers
 * that only need "a fresh run soon" should not dispatch while one is waiting.
 * Returns null if it cannot tell (then the caller dispatches, as before).
 */
export async function waitingRun() {
  const token = process.env.GITHUB_TOKEN;
  const repo = process.env.GITHUB_REPO;
  if (!token || !repo) return null;
  try {
    const r = await fetch(`${GITHUB_API}/repos/${repo}/actions/workflows/${WORKFLOW}/runs?per_page=10`, {
      headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json',
                 'X-GitHub-Api-Version': '2022-11-28' } });
    if (!r.ok) return null;
    const j = await r.json();
    return (j.workflow_runs || []).some(w => ['queued', 'pending', 'waiting', 'requested'].includes(w.status));
  } catch { return null; }
}

/**
 * Only accept values we generate ourselves or can fully validate — these reach a
 * shell step inside the workflow.
 */
export function cleanInputs(body = {}) {
  const inputs = {};
  if (typeof body.slate_date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(body.slate_date)) {
    inputs.slate_date = body.slate_date;
  }
  // slate_date is the only input the NHL workflow declares. GitHub rejects a dispatch
  // carrying any undeclared input (422), so MLB's window/full_pull are not passed on.
  return inputs;
}
