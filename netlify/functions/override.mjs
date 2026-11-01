// Manual overrides (order 4, Nov 2026): the admin panel's only way to write.
//
// POST /api/override  {secret, action, ...}
//   action "games"   today's games (ET slate) and the active overrides
//   action "roster"  {team}  that club's current goalies and skaters (official NHL feed)
//   action "set"     {team, player_id, kind: "goalie"|"status", value: "confirmed"|"out"|"in"}
//   action "remove"  {id}
// "set" and "remove" then dispatch a re-score run (run_kind manual_override: no Groq,
// no odds) so the boards pick the change up within a few minutes.
//
// Security:
//   - ADMIN_SECRET (Netlify env) is the passphrase. It is deliberately NOT the Update Now
//     secret: this one changes what subscribers see. Compared in constant time.
//   - Writes use SUPABASE_SERVICE_KEY (Netlify env), server side only. The anon key can
//     read manual_overrides but has no write grant or policy (db/overrides.sql).
//   - Today's slate only, and never once the game has started (the database trigger
//     refuses it too), so the Results freeze only ever sees pre-puck-drop overrides.
//   - Every value that reaches the database is validated here against the official
//     schedule and roster; nothing free-form is stored.

import { timingSafeEqual } from 'node:crypto';
import { dispatchWorkflow, waitingRun } from '../lib/github.mjs';

const NHL = 'https://api-web.nhle.com/v1';
const json = (body, status = 200) => Response.json(body, { status,
  headers: { 'Cache-Control': 'no-store' } });

function secretOk(given) {
  const want = process.env.ADMIN_SECRET;
  if (!want || typeof given !== 'string') return false;
  const a = Buffer.from(given), b = Buffer.from(want);
  return a.length === b.length && timingSafeEqual(a, b);
}

function easternDate(d = new Date()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(d);
}

async function sb(path, { method = 'GET', body, prefer } = {}) {
  const url = process.env.SUPABASE_URL?.replace(/\/$/, '');
  const key = process.env.SUPABASE_SERVICE_KEY;
  if (!url || !key) throw new Error('SUPABASE_URL / SUPABASE_SERVICE_KEY not set');
  const r = await fetch(`${url}/rest/v1/${path}`, {
    method,
    headers: { apikey: key, Authorization: `Bearer ${key}`, 'Content-Type': 'application/json',
               ...(prefer ? { Prefer: prefer } : {}) },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  if (!r.ok) throw new Error(`database ${r.status}: ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

async function todaysGames(day) {
  const r = await fetch(`${NHL}/schedule/${day}`);
  if (!r.ok) throw new Error(`schedule ${r.status}`);
  const j = await r.json();
  const out = [];
  for (const d of j.gameWeek || []) {
    if (d.date !== day) continue;
    for (const g of d.games || []) {
      if (![2, 3].includes(g.gameType)) continue;
      if (![undefined, null, 'OK'].includes(g.gameScheduleState)) continue;
      out.push({ game_id: g.id, away: g.awayTeam.abbrev, home: g.homeTeam.abbrev,
                 start: g.startTimeUTC,
                 started: Date.parse(g.startTimeUTC) <= Date.now() });
    }
  }
  return out;
}

async function roster(team) {
  const r = await fetch(`${NHL}/roster/${encodeURIComponent(team)}/current`);
  if (!r.ok) throw new Error(`roster ${team}: ${r.status}`);
  const j = await r.json();
  const p = (x, pos) => ({ player_id: x.id, pos,
    name: `${x.firstName?.default ?? ''} ${x.lastName?.default ?? ''}`.trim(),
    number: x.sweaterNumber ?? null });
  return {
    goalies: (j.goalies || []).map(x => p(x, 'G')),
    skaters: [...(j.forwards || []).map(x => p(x, x.positionCode || 'F')),
              ...(j.defensemen || []).map(x => p(x, 'D'))],
  };
}

const active = day => sb(`manual_overrides?slate_date=eq.${day}&removed_at=is.null` +
                         '&select=id,kind,team,player_id,player_name,value,game_start,set_at' +
                         '&order=set_at.asc');

async function rescore() {
  // Never displace a waiting run (it could be the Groq pre-game run): it reads the
  // overrides when it starts, so it already covers this change.
  if (await waitingRun()) return 'covered by the run already waiting to start';
  const res = await dispatchWorkflow({ run_kind: 'manual_override' });
  return res.ok ? 'dispatched' : `not dispatched (${res.status})`;
}

export default async (req) => {
  if (req.method !== 'POST') return json({ error: 'POST only' }, 405);
  let body;
  try { body = await req.json(); } catch { return json({ error: 'bad JSON' }, 400); }
  if (!secretOk(body.secret)) {
    await new Promise(r => setTimeout(r, 800));           // slow down guessing
    return json({ error: 'wrong passphrase' }, 401);
  }

  const day = easternDate();
  try {
    const games = await todaysGames(day);
    const gameOf = team => games.find(g => g.away === team || g.home === team);

    if (body.action === 'games') {
      return json({ slate_date: day, games, overrides: await active(day) });
    }

    if (body.action === 'roster') {
      const g = gameOf(body.team);
      if (!g) return json({ error: `${body.team} does not play today` }, 400);
      return json({ team: body.team, game: g, ...(await roster(body.team)) });
    }

    if (body.action === 'set') {
      const { team, kind, value } = body;
      const pid = Number(body.player_id);
      const ok = (kind === 'goalie' && value === 'confirmed') ||
                 (kind === 'status' && (value === 'out' || value === 'in'));
      if (!ok || !Number.isInteger(pid)) return json({ error: 'bad override' }, 400);
      const g = gameOf(team);
      if (!g) return json({ error: `${team} does not play today` }, 400);
      if (g.started) return json({ error: 'that game has started; overrides are closed' }, 409);
      const ro = await roster(team);
      const pool = kind === 'goalie' ? ro.goalies : ro.skaters;
      const player = pool.find(x => x.player_id === pid);
      if (!player) return json({ error: `not a ${kind === 'goalie' ? 'goalie' : 'skater'} on ${team}'s roster` }, 400);

      // replace the active override for the same team (goalie) or player (status)
      const same = kind === 'goalie' ? `team=eq.${team}` : `player_id=eq.${pid}`;
      const now = new Date().toISOString();
      await sb(`manual_overrides?slate_date=eq.${day}&kind=eq.${kind}&${same}&removed_at=is.null`,
               { method: 'PATCH', body: { removed_at: now }, prefer: 'return=minimal' });
      await sb('manual_overrides', { method: 'POST', prefer: 'return=minimal', body: {
        slate_date: day, kind, team, player_id: pid, player_name: player.name, value,
        game_id: g.game_id, game_start: g.start } });
      return json({ ok: true, run: await rescore(), overrides: await active(day) });
    }

    if (body.action === 'remove') {
      const id = Number(body.id);
      if (!Number.isInteger(id)) return json({ error: 'bad id' }, 400);
      const rows = await sb(`manual_overrides?id=eq.${id}&slate_date=eq.${day}&removed_at=is.null` +
                            '&select=game_start');
      if (!rows?.length) return json({ error: 'no such active override today' }, 404);
      if (Date.parse(rows[0].game_start) <= Date.now())
        return json({ error: 'that game has started; overrides are closed' }, 409);
      await sb(`manual_overrides?id=eq.${id}`, { method: 'PATCH', prefer: 'return=minimal',
                                                body: { removed_at: new Date().toISOString() } });
      return json({ ok: true, run: await rescore(), overrides: await active(day) });
    }

    return json({ error: 'unknown action' }, 400);
  } catch (e) {
    console.error(`override: ${e.message}`);
    return json({ error: e.message }, 500);
  }
};

export const config = { path: '/api/override' };
