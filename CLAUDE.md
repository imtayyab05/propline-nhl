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
- Odds: The Odds API, sport key `icehockey_nhl`. NHL has its OWN key on a separate
  Odds account from MLB, so its 500-credit monthly budget is NOT shared.

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

## Status (3 Oct 2026)

Schema is live in the NHL Supabase (run 3 Oct). Pipeline runs end to end locally and
publishes; dashboard (`web/index.html`) reads it on localhost:5174. Nothing committed
or pushed yet; no Netlify deploy yet.

Done for hockey: `propline/nhl.py`, `goalies.py`, `availability.py`, `teams.py`,
`scoring.py`, `output.py`, `publish.py`, `rationale.py` (hockey prompts; id-alignment
bug fixed — MLB still has it, flagged separately), `scripts/collect.py`,
`scripts/process.py`, `db/schema.sql`, `web/index.html`, `.github/workflows/daily.yml`
(drafted; Groq only on morning + pregame slots).

GitHub secrets added (4 Oct). Still to do before first deploy:
- Create the Netlify site from the repo with env: SUPABASE_URL, SUPABASE_ANON_KEY,
  GITHUB_REPO, GITHUB_BRANCH, GITHUB_TOKEN (fine-grained, propline-nhl only, Actions
  read/write), UPDATE_SECRET. Then one manual Update Now to prove the CI path.
- `scripts/healthcheck.py` is still MLB content and is not run by the workflow.
- `propline/odds.py` is MLB markets — Phase 2.

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
