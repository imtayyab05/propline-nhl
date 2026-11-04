// AI data feed (order 4, Nov 2026): GET /api/feed?date=YYYY-MM-DD[&format=md|json]&key=...
//
// Passphrase-gated since 4 Nov 2026 (Devin's request): FEED_SECRET in the Netlify env,
// passed as ?key= (for links given to AI tools) or the x-feed-key header (the dashboard's
// Copy for AI button). It is separate from ADMIN_SECRET, so a link handed to an AI tool
// never carries the power to change the boards. Read-only: ai_feed is read here with
// the service key, and the table is closed to the anon key (db/feed_lock.sql), so the
// feed cannot be pulled straight from Supabase with the public key either.
//
// Game status is overlaid live from the official NHL score feed at request time, so a
// model reading this never has to guess whether a game is scheduled, LIVE or final.
// Final scores are only ever the league's own; a game in progress is just "LIVE".
//
// format=md (default for the dashboard's "Copy for AI" button) is a compact Markdown
// rendering of the same JSON, sized for a chat model (target < ~4,000 tokens).

import { timingSafeEqual } from 'node:crypto';
import { toMarkdown } from '../lib/feedmd.mjs';

const NHL = 'https://api-web.nhle.com/v1';

function easternDate(d = new Date()) {
  return new Intl.DateTimeFormat('en-CA', { timeZone: 'America/New_York' }).format(d);
}

async function liveStatus(day) {
  try {
    const r = await fetch(`${NHL}/score/${day}`);
    if (!r.ok) return {};
    const j = await r.json();
    const out = {};
    for (const g of j.games || []) {
      const s = g.gameState;
      const status = ['LIVE', 'CRIT'].includes(s) ? 'LIVE'
                   : ['OFF', 'FINAL'].includes(s) ? 'final' : 'scheduled';
      out[g.id] = { status,
        ...(status === 'final' ? { final: `${g.awayTeam.abbrev} ${g.awayTeam.score}-${g.homeTeam.score} ${g.homeTeam.abbrev}`
                                          + (['OT', 'SO'].includes(g.gameOutcome?.lastPeriodType) ? ` (${g.gameOutcome.lastPeriodType})` : '') } : {}) };
    }
    return out;
  } catch { return {}; }
}

function keyOk(given) {
  const want = process.env.FEED_SECRET;
  if (!want || typeof given !== 'string' || !given) return false;
  const a = Buffer.from(given), b = Buffer.from(want);
  return a.length === b.length && timingSafeEqual(a, b);
}

const PRIVATE = { 'Cache-Control': 'private, no-store', 'Access-Control-Allow-Origin': '*' };

export default async (req) => {
  const u = new URL(req.url);
  if (!keyOk(u.searchParams.get('key') || req.headers.get('x-feed-key'))) {
    await new Promise(r => setTimeout(r, 600));            // slow down guessing
    return Response.json({ error: 'feed passphrase required (?key=...)' },
                         { status: 401, headers: PRIVATE });
  }
  const day = u.searchParams.get('date') || easternDate();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return Response.json({ error: 'date=YYYY-MM-DD' }, { status: 400 });
  const fmt = u.searchParams.get('format') === 'md' ? 'md' : 'json';

  const url = process.env.SUPABASE_URL?.replace(/\/$/, '');
  const svc = process.env.SUPABASE_SERVICE_KEY;          // ai_feed is closed to anon
  if (!url || !svc) return Response.json({ error: 'feed not configured' }, { status: 500 });
  const r = await fetch(`${url}/rest/v1/ai_feed?slate_date=eq.${day}&select=feed`, {
    headers: { apikey: svc, Authorization: `Bearer ${svc}` } });
  if (!r.ok) return Response.json({ error: `feed unavailable (${r.status})` }, { status: 502 });
  const rows = await r.json();
  if (!rows.length) return Response.json({ error: `no feed for ${day}` }, { status: 404 });

  const feed = rows[0].feed;
  const live = await liveStatus(day);
  for (const g of feed.games || []) Object.assign(g, live[g.id] || {});

  // private: the URL carries the passphrase, so no shared cache may keep a copy
  const headers = PRIVATE;
  return fmt === 'md'
    ? new Response(toMarkdown(feed), { headers: { ...headers, 'Content-Type': 'text/markdown; charset=utf-8' } })
    : Response.json(feed, { headers });
};

export const config = { path: '/api/feed' };
