# PropLine NHL

Automated daily NHL prop rankings for Fiverr client **devinmajor** (Bahamas). Sister
project to PropLine MLB, built on the same pattern but on **completely separate
platforms**: its own GitHub repo, its own Supabase project (on a DIFFERENT Supabase
account from MLB), its own Netlify site. Never merge the two or share code through a
common library unless the client explicitly asks — a change made for one sport must
never be able to break the other mid-season.

Reference implementation: `D:\Programming\Work\PropLine MLB` — read it for patterns
(`scripts/collect.py`, `scripts/process.py`, `propline/scoring.py`,
`propline/matchup.py`) rather than copying sport logic blindly.

## Order

Ordered and paid 30 Sep 2026, 21-day delivery, so **due ~21 Oct 2026**.
The NHL regular season began **29 Sep 2026** (the client was earlier told 12 Oct in
error), so "ready for opening week" has already passed — get Phase 1 in front of him
as early as possible. He said "we can tweak as we go".

## Agreed scope (as quoted to the client)

**Phase 1** — all five player props, total goals, and the team stats + power rankings
tab, on the same dashboard + Excel setup as MLB, automatic runs through the day.

**Phase 2** — team and game SOG props, team and game power-play-goal props, moneyline
and puck line ratings, and market lines alongside if he wants them (NHL lines need
their own odds budget — likely a paid plan, cost to him).

What his book (Bahamas) actually offers:
- Player: shots on goal (SOG), points, goals, assists, power-play points (PPP)
- Game: total SOG for the game and each team, total power-play goals (PPG) for the
  game and each team, moneyline, puck line, total goals
- Saves, blocks and hits historically NOT offered. Data covers them; keep optional.

How each is scored (what we told him):
- Player props: his own recent + season form (shot rate, ice time, PP time and role,
  line) against what the opponent gives up (shots allowed, goals allowed, penalty kill,
  how often they take penalties — the last drives PPP).
- Game/team props: both teams' for-and-against numbers, recent form, likely goalies.
- **Moneyline / puck line: a matchup-strength RATING shown beside the market line —
  never a win %.** We explicitly told him we would not show a probability we have not
  tested. Same principle as MLB totals: a ranking dressed up as a probability looks
  precise and means nothing.
- Team stats tab replaces what he used MoneyPuck for: goals, SOG, PPG, PP%, points —
  for and against, season and recent side by side — plus a power ranking.

**Client add-on (30 Sep):** when a starter who plays significant ice time is OUT, bump
the remaining players who absorb his minutes — especially if he was offensive; it has
implications at both ends of the ice. Needs injury/scratch data — see open problems.

## Data sources

- **Official NHL feed, no key:**
  - `https://api-web.nhle.com/v1/schedule/{date}` — weekly schedule, `gameType` 1 =
    preseason, 2 = regular, 3 = playoffs
  - `.../gamecenter/{id}/boxscore` — per player: goals, assists, points, `sog`, hits,
    `blockedShots`, `toi`, `powerPlayGoals`, shifts; goalies: `saves`, `shotsAgainst`,
    `starter` (post-game only), PP/SH/EV splits
  - `.../gamecenter/{id}/play-by-play` — every shot with x/y, shot type, zone and
    `situationCode` (strength state) → build shot quality and PP role ourselves
  - `https://api.nhle.com/stats/rest/en/skater/summary` — `ppPoints`, `ppGoals`,
    `shots`, `timeOnIcePerGame`; `.../team/summary` — goals/shots for & against per
    game, `powerPlayPct`, `penaltyKillPct`
- **MoneyPuck is BLOCKED.** Every URL including the CSVs returns 403 with
  `Cf-Mitigated: challenge` (Cloudflare bot challenge). Do not scrape it and do not try
  to get around the challenge. We told the client we build from the official feed —
  the same raw data MoneyPuck builds from — and that our shot-quality measure is our
  own, not a copy of theirs.
- Odds: The Odds API, sport key `icehockey_nhl`. If the same key as MLB is used, the
  500-credit monthly budget is SHARED with MLB.

## Open problems — solve before promising behaviour

1. **Starting goalies.** The feed marks `starter` only after the game. We told him the
   system projects the likely starter from recent usage (last starts, back-to-backs,
   rest days) and labels it PROJECTED until confirmed. A confirmation source is TBD.
2. **Injuries / scratches** (needed for his add-on). Not in the official feed pre-game.
   Find a source that permits automated access before designing around it.
3. **Run schedule.** The copied Netlify scheduled functions still carry MLB's five
   slots (13:07, 15:37, 18:07, 21:07, 23:07 UTC). NHL games are mostly evenings ET and
   goalies firm up on game day — redesign the slots before the first deploy.

## Copied from MLB on 2 Oct 2026 — status of each file

Generic, usable as-is: `propline/db.py`, `propline/storage.py`,
`propline/intermediate.py`, `netlify/lib/github.mjs`, `netlify/functions/config.mjs`,
`netlify/functions/trigger.mjs`, `api/*`, `netlify.toml`, `requirements.txt`,
`.gitignore`, `.claude/launch.json` (port 5174 so it runs beside MLB's 5173).

Needs NHL adaptation — the engine is reusable, the baseball content is not:
- `propline/publish.py` — `PROP_DETAIL` / `PITCHER_DETAIL` / `GAME_DETAIL` are MLB fields
- `propline/rationale.py` — `SYSTEM` / `TOTALS_SYSTEM` prompts describe baseball
- `propline/odds.py` — `SPORT` already set to `icehockey_nhl`; markets are still MLB's
  (`pitcher_strikeouts`), and the budget comments describe MLB
- `propline/output.py` — per-prop Excel columns are MLB
- `scripts/healthcheck.py` — strikeout-line and arsenal checks are MLB
- `db/schema.sql` — `bullpen_status` is MLB-only (NHL equivalent: goalie status)
- `web/index.html` — prop tabs, column sets and the strikeout arsenal expander are MLB
- `.github/workflows/daily.yml` and `netlify/functions/scheduled-slate*.mjs` — slots

Not copied (sport-specific, rebuild for hockey): collect/process scripts, Savant,
mlb.py, rolling, profiles, matchup, scoring, arsenal, weather (all NHL arenas indoor).

## Principles carried over from MLB

- Rankings are percentiles within the day's slate, not predictions. The AI writes the
  one-line "Why" text only; it never ranks anything.
- Silent failure is the enemy. Every upstream source has changed or broken without
  warning at least once. Flag coverage gaps visibly (like MLB's `park_matched`,
  `starters_resolved`) instead of quietly scoring missing data as average.
- Off-days and TBD starters are normal, not errors — exit cleanly and log why.
- Publish deletes a slate before inserting it, so `db.check_json()` must run first.
- Measure coverage against what the system attempts, not against everything stored.

## Environment and conventions

- Python is NOT on PATH: use `D:/Programming/Anaconda/python.exe`. Bare `python` opens
  the Windows Store stub and hangs.
- No `gh` CLI and no GitHub token locally — read run logs from `pipeline_runs`.
- Repo stays PRIVATE. The client will not add payment methods for any service.
- One scheduler only (Netlify). Never add GitHub's `schedule:` back alongside it.
- Batch pushes: Netlify deploys cost credits, and the `ignore` rule in `netlify.toml`
  skips deploys when `web/`, `netlify/` and `netlify.toml` are unchanged.
- Account-level quotas are SHARED with MLB even though the projects are separate:
  GitHub Actions minutes (2,000/month across all private repos), Netlify credits
  (300/month across all sites on the team), and Groq/Odds if the same keys are reused.
