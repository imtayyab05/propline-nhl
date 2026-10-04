// The morning slot, 12:07 UTC (08:07 ET in summer time, 07:07 ET in winter).
//
// Last night's games are final and in the stats feed by now, so this is the day's
// first full read: fresh form windows, projected goalies, who is out. One of the two
// slots that writes the AI "Why" text (see EXPLAIN in .github/workflows/daily.yml).
//
// A separate function from scheduled-slate.mjs only because Netlify allows one
// schedule per function and this one lands at :07, the others at :37.
//
// Netlify is the ONLY scheduler. Never add `schedule:` to the GitHub workflow too —
// on MLB both fired and the free Actions minutes were gone by the 18th.

import { dispatchWorkflow } from '../lib/github.mjs';

export default async () => {
  const res = await dispatchWorkflow({ run_kind: 'scheduled_morning' });

  if (res.ok) {
    console.log('morning slate dispatched');
    return new Response('dispatched', { status: 202 });
  }

  console.error(`dispatch failed: ${res.status} ${res.detail ?? ''} ${res.hint ?? ''}`);
  return new Response(`dispatch failed: ${res.status}`, { status: 500 });
};

export const config = { schedule: '7 12 * * *' };
