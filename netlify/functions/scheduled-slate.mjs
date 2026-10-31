// The 22:37 UTC pre-game slot. The morning slot is scheduled-slate-morning.mjs.
//
// Since 1 Nov 2026 this is the only fixed game-day slot. The old 17:37 midday and 01:37
// late slots are replaced by schedule-driven wave runs (scheduled-waves.mjs), which land
// 75-90 minutes before each group of puck drops: 17:37 was 37 min AFTER a 1pm ET start
// in summer time and missed 12:30pm starts outright.
//
// 22:37 stays because it is the run that writes the AI "Why" text and pulls the full
// odds (team totals) close to the evening games. In winter time it is 83 min before a
// 7pm ET start and stands in for that wave; in summer time (23 min before) the 7pm
// wave runs as well, earlier, and this run refreshes it.
//
// Netlify is the ONLY scheduler — see scheduled-slate-morning.mjs.

import { dispatchWorkflow } from '../lib/github.mjs';

export default async () => {
  const res = await dispatchWorkflow({ run_kind: 'scheduled_pregame' });

  if (res.ok) {
    console.log('pre-game slate dispatched');
    return new Response('dispatched', { status: 202 });
  }

  console.error(`dispatch failed: ${res.status} ${res.detail ?? ''} ${res.hint ?? ''}`);
  return new Response(`dispatch failed: ${res.status}`, { status: 500 });
};

export const config = { schedule: '37 22 * * *' };
