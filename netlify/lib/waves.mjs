// Game-day "waves" (1 Nov 2026): runs that follow the NHL schedule instead of fixed
// clock times.
//
// Why: the fixed 17:37 UTC midday run was 37 min AFTER a 1pm ET puck drop in summer
// time and only 23 min before it in winter time, and missed 12:30pm games outright, so
// Devin had to press Update Now on weekends. Rosters post ~2h before puck drop (measured
// 3 Oct), so a run 75-90 min before each start catches them in either season.
//
// The rule, mirrored in propline/waves.py (health check + simulation). Keep them equal.
//  1. Distinct start times of the slate, earliest first. A wave starts at the earliest
//     start not yet covered and takes every start within WAVE_SPAN after it.
//  2. A wave is dispatched on the first 15-minute tick at or after (anchor - LEAD).
//     The scheduled function runs every 15 min, so exactly one tick falls in that slot:
//     a stateless "one run per wave, never twice".
//  3. A wave is skipped when a fixed run already lands between LEAD and MIN_LEAD
//     before its anchor (the 22:37 pre-game run for 7pm ET games in winter time, the
//     12:07 morning run for a morning Global Series game). Those fixed runs carry Groq
//     and odds; a wave would only duplicate them.

export const TICK_MIN = 15;     // the scheduled function's cadence
export const LEAD_MIN = 90;     // dispatch this long before the wave's first puck drop
export const MIN_LEAD_MIN = 30; // a fixed run closer than this does not count as cover
export const WAVE_SPAN_MIN = 30;

const MIN = 60 * 1000;

/** Fixed runs for a slate dated `day` (YYYY-MM-DD, US Eastern), as UTC ms. */
export function fixedRuns(day) {
  const d = Date.parse(`${day}T00:00:00Z`);
  return [d + (12 * 60 + 7) * MIN,      // morning, Groq + odds
          d + (22 * 60 + 37) * MIN];    // pre-game, Groq + full odds
}

/** Waves for one slate. `starts` = game start times (ms, UTC). */
export function planWaves(day, starts) {
  const s = [...new Set(starts.filter(Number.isFinite))].sort((a, b) => a - b);
  const fixed = fixedRuns(day);
  const waves = [];
  for (const t of s) {
    const last = waves.at(-1);
    if (last && t - last.anchor <= WAVE_SPAN_MIN * MIN) { last.starts.push(t); continue; }
    waves.push({ anchor: t, starts: [t] });
  }
  for (const w of waves) {
    const tick = TICK_MIN * MIN;
    w.dispatch = Math.ceil((w.anchor - LEAD_MIN * MIN) / tick) * tick;
    const cover = fixed.find(f => f >= w.anchor - LEAD_MIN * MIN &&
                                  f <= w.anchor - MIN_LEAD_MIN * MIN);
    w.skip = cover !== undefined ? 'covered by a fixed run' : null;
  }
  return waves;
}

/** The wave (if any) whose dispatch tick is the one containing `now`. */
export function dueWave(day, starts, now) {
  const tick = Math.floor(now / (TICK_MIN * MIN)) * TICK_MIN * MIN;
  return planWaves(day, starts).find(w => !w.skip && w.dispatch === tick) || null;
}
