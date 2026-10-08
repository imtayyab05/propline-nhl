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

**Delivery plan (Tayyab, 5 Oct) - DONE:** Phase 1 + 2 were delivered together on 7 Oct.
The site is now the client's; messages may link to it.

**Client decisions and wishes (4 Oct, before seeing anything):**
- Market lines: GAME LINES ONLY (ML, puck line, total, team totals) on the free Odds
  tier "until I / we move to a subscription selling service". No player prop lines.
- His book is Island Luck (islandluck.com). Player props there are MILESTONE yes/no
  markets: anytime goal (and first goal), 2+/3+ goals, 1+/2+ points, 1+ assists; puck
  line offered at +-1.5 and +-2.5; totals include OT. Speak in those terms.
- Ideas he floated, explicitly "not necessarily on this initial make" (NOT in the
  quoted scope — Tayyab decides whether they are tweaks or a paid add-on):
  safe vs aggressive SOG threshold per player (usage, PP unit, TOI); 2+ points
  candidates; hot/cold streaks, head-to-head, DvP, a "pro player"/star-power factor.
  Shared Titan/Magnus "10,000 simulation" boards (ML/PL/OU) as inspiration. Checked
  5 Oct against official results for 1-3 Oct: Titan PRIME totals 9/15 while printing
  88-99.7% win probabilities; Magnus PRIME moneylines 7/14 while printing 53-96%.
  Their stated probabilities are not calibrated — supports our "rating, never win %".

**Delivered 7 Oct 2026** (Phase 1 + 2 together); Devin accepted it on 7 Oct. The
maintenance retainer moves to $75/month for MLB + NHL.

**Order 2: trend features — $200, 7-day delivery, paid ~8 Oct 2026, due ~15 Oct.**
Built only from the game logs already collected (no new paid data, no odds credits):
safe and aggressive SOG line (1+/2+/3+/4+ cleared in last 10 / last 20 / season;
safe = cleared in 8+ of 10, aggressive = about half), 2+ point spots, multi-goal (2+
goals, info only), hot/cold streaks (points, shots, goals; last 5 vs season),
head-to-head vs tonight's opponent (2-3 past seasons pulled once and cached; info
only), DvP (shots, points, goals allowed to C / W / D, season and last 10). Every one
shown as a plain track record ("2+ shots in 8 of last 10"), never a percentage, in his
book's terms (1+/2+ points, anytime/2+ goals, 1+ assists). Promised free alongside it
(Tayyab's 8 Oct reply): back-to-back penalty measured from last season, home and road
separately; a warning on picks that depend on a "lean" goalie.

Order 2 DELIVERED 8 Oct 2026 (guide v2 PDF attached; first CI runs with the trend code
passed 7-8 Oct, goalies confirmed live). Order 2 status (8 Oct): BUILT and live (propline/trends.py; tabs SOG Lines, 2+ Points,
Multi-Goal, Streaks, Head-to-Head, DvP). Backtest on 14,508 player-games since 1 Mar
2026: safe line cleared next game 77%, aggressive 47%; 3+ two-point games in last 10
-> 2+ points next game 24% (vs 5% with none). Those figures are a check of the METHOD
for the guide; the site itself shows only "x of y" track records. Head-to-head history
(2023-24, 2024-25) is cached in data/cache/{season}/skater_history.csv and in Actions.
Free fixes live: measured back-to-back (road -0.23, home -0.20 units on 8 Oct) and the
Goalie Check column. Client guide (delivery/, git-ignored) updated to v2.

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
- Odds: The Odds API, sport key `icehockey_nhl`. NHL has its OWN key on a separate
  Odds account from MLB, so its 500-credit monthly budget is NOT shared.
  Measured 4 Oct 2026 (docs + 1-credit `/events/{id}/markets` probe, 11 US books):
  - Free: `/sports`, `/events`. `/odds` (whole slate) = markets x regions per call, so
    h2h+spreads+totals = 3 per call. `/events/{id}/odds` = markets RETURNED x regions,
    per game. Empty responses cost 0.
  - On offer for NHL: h2h, spreads (puck line), totals (11 books), team_totals (5),
    player_shots_on_goal (3), player_points (2), player_goals (2), player_assists (1),
    PPP only as an alternate market (1 book). NOT offered at all: game/team SOG totals
    and game/team PP-goal totals — those Phase 2 props get ratings with no market line.
  - Budget at ~30 slates / ~220 games a month: game lines 1x/day = 90, 2x/day = 180;
    team totals 1x/day ~220; each player-prop market 1x/day ~220; all five ~1,000+.
    Paid plans: 20K credits $30/mo, 100K $59/mo.

## Data decisions (settled 2 Oct 2026) and what is still open

1. **Starting goalies — official feed only** (`propline/goalies.py`). Projected from
   usage: current-roster goalies, starts in the team's last 10, last season's workload
   as a prior while the season is young, back-to-back -> backup. Labelled PROJECTED with
   confidence high/lean. If tonight's game roster is posted and omits him, switch.
   CONFIRMED only from the boxscore `starter` flag (i.e. once the game is on).
   Rejected: DailyFaceoff (robots allows the page, but no published terms granting
   automated use; it is a commercial site), ESPN (terms forbid automated access; its
   API host 403s robots.txt), Rotowire (commercial). Revisit only if Tayyab decides.
2. **Injuries / scratches — official feed only** (`propline/availability.py`). A
   current-roster skater who did not dress for his team's latest game(s) is OUT until
   he plays again. Tonight's game-day list (play-by-play `rosterSpots`) then refines it.
   **Measured 3 Oct 2026** (13 games, `scripts/watch_rosters.py`): the list posts
   96-156 min before puck drop (~2h) as an EXTENDED list of 43-46 names (~23 per team,
   extras and 3rd goalies included), and is trimmed to the 40 who dress only later
   (post-game it matched the boxscore exactly). So: NOT on the list = OUT (certain);
   on an extended list = still projected; on a list of <=20 per team = confirmed.
   Known blind spot: a same-day injury is unknown until the list posts.
3. **Run schedule (set 4 Oct 2026)** — Netlify, UTC, chosen to hold across the 1 Nov
   US clock change: 12:07 morning (Groq), 17:37 midday, 22:37 pregame (Groq), 01:37
   late. The workflow dates the slate in US Eastern time so the 01:37 run is "tonight".

Stats API trap: every response is capped at 10,000 rows and `total` then reads 10,000,
so a capped pull looks complete. `nhl._stats` raises at the cap; skater logs are pulled
in 14-day chunks. Last season's logs are cached in `data/cache/{season}/`.

## Status (4 Oct 2026) — LIVE

Site: https://propline-nhl.netlify.app (Netlify, same team as MLB). Repo is public.
First CI run via Update Now succeeded 4 Oct (run 37196350513, ~1 min, 0 billable
minutes). Scheduled slots go live from the next slot after deploy.

Fixed on the way: GitHub secrets with a trailing newline broke the auth header —
`db.env()` now strips all secrets. GitHub token must be fine-grained, only
propline-nhl, Repository permission "Actions: Read and write" (repo access and the
permission must be saved in the same edit or GitHub drops the repo selection).

Still open:
- Watch the first scheduled runs (midday 17:37, pregame 22:37 with Groq, late 01:37,
  morning 12:07 with Groq) in pipeline_runs.
- Client guide + message to Devin when Tayyab is ready.
- Phase 2 BUILT 6 Oct (`propline/games.py`, `propline/odds.py`): Moneyline, Puck Line,
  Team Goals, Game/Team SOG, Game/Team PPG boards + game lines (ML, PL, total, team
  totals) in table `market_lines` (upserted, never cleared; in-play lines skipped).
  Odds pulled on morning (`--odds game`, 3 credits) and pregame (`--odds full`, +~1 per
  game); other runs reuse stored lines. Home ice is MEASURED from last season each run
  (0.127 strength units on 6 Oct), not guessed. Needs: market_lines SQL run in Supabase
  and ODDS_API_KEY added as a GitHub secret.

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
- The NHL repo is PUBLIC by Tayyab's decision (4 Oct 2026). Do not re-raise it. It
  means nothing secret may ever be committed: keys stay in `.env` / GitHub secrets /
  Netlify env, and generated data stays git-ignored. Scan staged diffs for keys
  before every commit. The client will not add payment methods for any service.
- One scheduler only (Netlify). Never add GitHub's `schedule:` back alongside it.
- Batch pushes: Netlify deploys cost credits, and the `ignore` rule in `netlify.toml`
  skips deploys when `web/`, `netlify/` and `netlify.toml` are unchanged.
- Account-level quotas are SHARED with MLB even though the projects are separate:
  GitHub Actions minutes (2,000/month across private repos — NHL is public, so its
  runs do not draw on that pool), Netlify credits
  (300/month across all sites on the team), and Groq (same key as MLB, confirmed 2 Oct 2026).
  Odds is NOT shared: separate accounts, like Supabase.
