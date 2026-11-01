// Markdown rendering of the AI data feed (order 4). Used by functions/feed.mjs;
// kept here so it can be run locally to measure the feed's size.

const BOARD_LABEL = { points: 'Points 1+', goals: 'Goals anytime', assists: 'Assists 1+',
                      ppp: 'PP points 1+', sog: 'Shots on goal' };
const TR_LABEL = { goals: 'Goals anytime', multi_goal: 'Multi-goal 2+', points: 'Points 1+',
  multi_point: '2+ points', assists: 'Assists 1+', ppp: 'PP points 1+', sog_safe: 'SOG safe line',
  sog_aggressive: 'SOG aggressive line', total_goals: 'Total goals model lean',
  team_goals_tag: 'Team total tag', team_goals_total: 'Team over market TT',
  moneyline: 'Moneyline stronger side', puck_line: 'Puck line covered' };

const n = (v, d = 2) => v == null ? '-' : Number(v).toFixed(d).replace(/^0\./, '.').replace(/^-0\./, '-.');
const pr = v => v == null ? '-' : (v > 0 ? `+${v}` : `${v}`);
// goalie status as short codes (legend in the Teams header)
const gs = s => !s ? '' : s.startsWith('confirmed (manual') ? 'conf-manual'
  : s === 'confirmed' ? 'conf' : s.includes('high') ? 'proj-firm' : s.includes('lean') ? 'proj-lean' : 'proj';
const line = s => s ? s.replace(/ \(\d+\/\d+\)/g, '') : '-';
const pg = a => (a || []).map(v => v == null ? '-' : n(v, 2)).join('/');
const sg = a => (a || []).map(v => v == null ? '-' : n(v, 1)).join('/');

export function toMarkdown(f) {
  const L = [];
  L.push(`# PropLine NHL data feed: ${f.slate_date}`);
  L.push(`Generated ${f.generated_at} (${f.run}). ${f.note}`);
  L.push(`TEAMS PLAYING TODAY (only these): ${f.teams_today.join(', ')}`);
  L.push('');
  L.push('## Games');
  L.push('time ET | game | status | proj total | market total (over/under) | model lean | stronger side (edge) | market fav | PL');
  for (const g of f.games) {
    const st = g.final ? `final ${g.final}` : g.status;
    L.push(`${g.time_et} | ${g.away} @ ${g.home} | ${st} | ${n(g.proj_total)} | ${g.mkt_total ?? '-'} (${pr(g.over)}/${pr(g.under)}) | ${g.lean ?? '-'} | ${g.stronger ?? '-'} (${g.edge ?? '-'}) | ${g.mkt_fav ?? '-'} | ${g.pl ?? '-'}`);
  }
  L.push('');
  L.push('## Teams (proj goals | tag | mkt TT | ML | B2B | goalie, adj SV%, conf/conf-manual/proj-firm/proj-lean | out)');
  for (const g of f.games) {
    for (const t of [g.away, g.home]) {
      const x = g.teams[t] || {}, gk = x.goalie || {};
      L.push(`${t}: ${n(x.proj)}${x.tag ? ` | ${x.tag.replace('Team Total ', 'TT ')}` : ''}` +
        `${x.mkt_tt != null ? ` | mkt ${x.mkt_tt}` : ''} | ML ${pr(x.ml)}${x.b2b ? ' | B2B' : ''}` +
        ` | ${gk.name ?? '?'} ${gk.sv != null ? n(gk.sv, 3) : ''} ${gs(gk.status)}${gk.weak_sv ? ' SV<=.888' : ''}` +
        `${x.out ? ` | out: ${x.out.join(', ')}` : ''}`);
    }
  }
  L.push('');
  L.push('## Players');
  L.push('name | team-opp | pos | TOI L5/L10 | PP TOI | Pts/60 | Ast/60 | Pts/G L3/L5/L10/season | SOG/G L3/L5/L10/season | SOG lines | flags');
  for (const p of Object.values(f.players)) {
    L.push(`${p.name} | ${p.team}-${p.opp} | ${p.pos ?? ''} | ${sg(p.toi)} | ${p.pp_toi ?? '-'} | ${n(p.pts60)} | ${n(p.ast60)} | ${pg(p.pts_pg)} | ${sg(p.sog_pg)} | ${line(p.sog_line)} | ${(p.flags || []).join('; ') || '-'}`);
  }
  L.push('');
  L.push(`## Boards (top ${Object.values(f.boards)[0]?.length ?? 10}: rank. player (PropLine score 0-100))`);
  for (const [b, rows] of Object.entries(f.boards)) {
    L.push(`${BOARD_LABEL[b] || b}: ` + rows.map(([id, rank, sc]) => `${rank}. ${f.players[id]?.name ?? id} (${sc})`).join(', '));
  }
  L.push('');
  L.push(`## Low-TOI efficiency (${f.low_toi_rule})`);
  L.push(f.low_toi.length ? f.low_toi.map(id => f.players[id]?.name ?? id).join(', ') : 'none tonight');
  const tr = f.track_record || {};
  if (tr.boards && Object.keys(tr.boards).length) {
    L.push('');
    L.push(`## PropLine track record, top 10 per board (since ${tr.since}): hits/graded (base rate)`);
    for (const [b, v] of Object.entries(tr.boards)) {
      const [h, g, base] = v.top10_30d;
      L.push(`${TR_LABEL[b] || b}: last 30d ${h}/${g} = ${g ? Math.round(100 * h / g) : '-'}% (base ${base != null ? Math.round(100 * base) + '%' : '-'})`);
    }
  }
  return L.join('\n');
}

