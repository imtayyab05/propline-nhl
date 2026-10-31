// Game-day wave runs (1 Nov 2026): a run 75-90 minutes before each group of puck drops,
// read from the NHL schedule, instead of the old fixed 17:37 and 01:37 UTC slots.
// The rule lives in ../lib/waves.mjs. Wave runs are labelled scheduled_wave and call
// neither Groq nor the Odds API (daily.yml decides that from the label).
//
// Every 15 minutes this reads the day's schedule (one small GET to the free NHL feed,
// memoised while the function instance is warm) and dispatches only when the current
// tick is a wave's dispatch tick, so each wave fires once and only once. Outside the
// hours a wave can fall in, it returns without fetching anything.
//
// Netlify is the ONLY scheduler. Never add `schedule:` to the GitHub workflow too.

import { dispatchWorkflow } from '../lib/github.mjs';
import { dueWave } from '../lib/waves.mjs';

const NHL = 'https://api-web.nhle.com/v1';
const cache = new Map();                 // ET date -> {at, starts}
const CACHE_MS = 30 * 60 * 1000;

function easternDate(now) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(now);
}

async function startTimes(day) {
  const hit = cache.get(day);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.starts;
  const r = await fetch(`${NHL}/schedule/${day}`);
  if (!r.ok) throw new Error(`schedule ${day}: ${r.status}`);
  const j = await r.json();
  const starts = [];
  for (const d of j.gameWeek || []) {
    if (d.date !== day) continue;
    for (const g of d.games || []) {
      // regular season and playoffs only; postponed / cancelled games never get a run
      if (![2, 3].includes(g.gameType)) continue;
      if (![undefined, null, 'OK'].includes(g.gameScheduleState)) continue;
      starts.push(Date.parse(g.startTimeUTC));
    }
  }
  cache.set(day, { at: Date.now(), starts });
  return starts;
}

export default async () => {
  const now = Date.now();
  const day = easternDate(new Date(now));
  let wave;
  try {
    wave = dueWave(day, await startTimes(day), now);
  } catch (e) {
    console.error(`wave check failed: ${e.message}`);
    return new Response('schedule unavailable', { status: 502 });
  }
  if (!wave) return new Response('no wave due', { status: 200 });

  const first = new Date(wave.anchor).toISOString().slice(11, 16);
  const res = await dispatchWorkflow({ run_kind: 'scheduled_wave' });
  if (res.ok) {
    console.log(`wave dispatched for ${wave.starts.length} start time(s) from ${first} UTC`);
    return new Response('dispatched', { status: 202 });
  }
  console.error(`wave dispatch failed: ${res.status} ${res.detail ?? ''} ${res.hint ?? ''}`);
  return new Response(`dispatch failed: ${res.status}`, { status: 500 });
};

// Every 15 minutes during the hours a wave can be due: from about 6:30am ET (a morning
// Global Series game in Europe) to 9:30pm ET (10:30pm West Coast games), i.e. 10:00-04:59
// UTC across both summer and winter time. Nothing runs 05:00-09:59 UTC.
export const config = { schedule: '*/15 0-4,10-23 * * *' };
