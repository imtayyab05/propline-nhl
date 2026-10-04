// The three game-day slots at :37. The morning slot is scheduled-slate-morning.mjs.
//
// Timed to when the league posts each game's roster, which is what turns "projected"
// into "known out". Measured on 3 Oct 2026 (13 games, scripts/watch_rosters.py): the
// list appears 96-156 minutes before puck drop, about two hours as a rule.
//
//   17:37 UTC  midday   weekend / holiday matinees have their lists by now
//   22:37 UTC  pregame  7pm ET games' lists are up (23 min out in summer time,
//                       83 min out in winter time). Writes the AI "Why" text.
//   01:37 UTC  late     10pm ET West Coast games' lists are up in either season
//
// Slots were chosen to work on BOTH sides of the US clock change (1 Nov): Netlify's
// schedule is in UTC, while puck drop is fixed in Eastern time and moves an hour later
// in UTC over the winter. The 01:37 run is after midnight UTC but still "tonight" in
// Eastern time; the workflow dates the slate in Eastern time for exactly that reason.
//
// Netlify is the ONLY scheduler — see scheduled-slate-morning.mjs.

import { dispatchWorkflow } from '../lib/github.mjs';

// Which slot this invocation is. The workflow cannot work it out for itself (it is
// dispatched, not scheduled), and it decides the run label and whether Groq is called.
const SLOTS = {
  17: 'scheduled_midday',
  22: 'scheduled_pregame',
  1: 'scheduled_late',
};

export default async () => {
  const hour = new Date().getUTCHours();
  const runKind = SLOTS[hour];

  if (!runKind) {
    // Fired outside its declared hours. Run anyway — a mislabelled run beats a missed
    // slate — but say so, because it means the schedule has drifted.
    console.warn(`unexpected dispatch hour ${hour}:00 UTC; running unlabelled`);
  }

  const res = await dispatchWorkflow(runKind ? { run_kind: runKind } : {});

  if (res.ok) {
    console.log(`slate dispatched (${runKind ?? 'unlabelled'})`);
    return new Response('dispatched', { status: 202 });
  }

  console.error(`dispatch failed: ${res.status} ${res.detail ?? ''} ${res.hint ?? ''}`);
  return new Response(`dispatch failed: ${res.status}`, { status: 500 });
};

export const config = { schedule: '37 1,17,22 * * *' };
