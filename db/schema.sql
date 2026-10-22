-- PropLine NHL — database schema
-- Run this ONCE in the Supabase SQL Editor of the NHL project
-- (left sidebar -> SQL Editor -> New query -> paste -> Run).
-- Safe to re-run: everything is IF NOT EXISTS / drop-and-recreate policies.

-- ============================================================ core reference

create table if not exists players (
    player_id     bigint primary key,          -- NHL player id
    player_name   text,
    team          text,                        -- current club abbreviation, e.g. BOS
    position      text,                        -- C | L | R | D | G
    updated_at    timestamptz default now()
);

-- ============================================================ slate

create table if not exists games (
    game_id         bigint primary key,        -- NHL game id, e.g. 2026020017
    game_date       date not null,
    game_time_utc   timestamptz,
    game_type       int,                       -- 2 regular season | 3 playoffs
    state           text,                      -- FUT | PRE | LIVE | OFF | FINAL
    venue           text,
    home_team       text,
    home_team_id    int,
    away_team       text,
    away_team_id    int,
    updated_at      timestamptz default now()
);
create index if not exists games_date_idx on games (game_date);

-- Who we expect in net. Replaces MLB's bullpen_status.
create table if not exists goalie_starts (
    game_date       date not null,
    game_id         bigint not null,
    team_id         int not null,
    team            text,
    goalie_id       bigint,
    goalie_name     text,
    status          text,                      -- projected | confirmed
    confidence      text,                      -- high | lean | confirmed | none
    reason          text,                      -- plain English, shown on the dashboard
    starts_recent   int,
    back_to_back    boolean,
    backup_id       bigint,
    backup_name     text,
    sv_pct          numeric,                   -- shrunk save %, what scoring used
    updated_at      timestamptz default now(),
    primary key (game_id, team_id)
);
create index if not exists goalie_starts_date_idx on goalie_starts (game_date);

-- Regulars who are out tonight, and why. Drives the minutes-bump add-on.
create table if not exists player_status (
    game_date       date not null,
    team            text not null,
    player_id       bigint not null,
    player_name     text,
    position        text,
    base_toi        numeric,                   -- usual minutes per game
    games_missed    int,
    status          text,                      -- out | in
    status_source   text,                      -- game_roster | recent_games | none
    reason          text,
    updated_at      timestamptz default now(),
    primary key (game_date, team, player_id)
);

-- ============================================================ output

create table if not exists prop_picks (
    slate_date     date not null,
    prop           text not null,              -- sog | points | goals | assists | ppp
    subject_id     bigint not null,            -- player_id
    subject_name   text,
    team           text,
    opponent       text,
    rank           int,
    score          numeric,                    -- 0-100 percentile score within the slate
    lineup_status  text,                       -- projected | confirmed (game roster posted)
    rationale      text,                       -- AI-written; the ranking is NOT from the AI
    details        jsonb,                      -- the signals behind the score
    created_at     timestamptz default now(),
    primary key (slate_date, prop, subject_id)
);
create index if not exists prop_picks_date_idx on prop_picks (slate_date, prop, rank);

create table if not exists game_picks (
    slate_date      date not null,
    game_id         bigint not null,
    prop            text not null,             -- total_goals (Phase 2 adds sog, ppg, ml, pl)
    subject         text,                      -- matchup label or team
    rank            int,
    score           numeric,
    goalies_status  text,                      -- e.g. "projected / confirmed"
    rationale       text,
    details         jsonb,
    created_at      timestamptz default now(),
    primary key (slate_date, prop, game_id, subject)
);

-- Team stats + power rankings tab (what the client used MoneyPuck for).
-- Season and last-10 side by side. "season" is THIS season; early on, the power score
-- leans on last season until enough games are played (see propline/teams.py).
create table if not exists team_stats (
    slate_date        date not null,
    team_id           int not null,
    team              text,
    team_name         text,
    power_rank        int,
    power_score       numeric,
    gp                int,
    record            text,                    -- W-L-OTL
    points            int,
    points_pct        numeric,
    gf_pg             numeric,  ga_pg             numeric,
    sf_pg             numeric,  sa_pg             numeric,
    ppg_pg            numeric,  ppga_pg           numeric,
    pp_pct            numeric,  pk_pct            numeric,
    pen_taken_pg      numeric,                 -- times shorthanded per game
    l10_gp            int,
    l10_record        text,
    l10_gf_pg         numeric,  l10_ga_pg         numeric,
    l10_sf_pg         numeric,  l10_sa_pg         numeric,
    l10_ppg_pg        numeric,  l10_ppga_pg       numeric,
    l10_pp_pct        numeric,  l10_pk_pct        numeric,
    l10_pen_taken_pg  numeric,
    details           jsonb,
    created_at        timestamptz default now(),
    primary key (slate_date, team_id)
);

-- Market lines for the game boards (Phase 2): moneyline, puck line, total, team totals.
-- Consensus across US books (median price at the most-quoted line). Rows are UPSERTED,
-- never cleared, so a game that has started keeps its last pre-game line.
-- market: moneyline | puck_line | total | team_total
-- subject: team abbreviation, or 'game' for the game total. side: over | under | ''.
create table if not exists market_lines (
    slate_date   date not null,
    game_id      bigint not null,
    market       text not null,
    subject      text not null,
    side         text not null default '',
    point        numeric,
    price        int,                         -- American odds
    books        int,
    fetched_at   timestamptz,
    primary key (slate_date, game_id, market, subject, side)
);
create index if not exists market_lines_date_idx on market_lines (slate_date);

-- ============================================================ run log

create table if not exists pipeline_runs (
    id            bigserial primary key,
    slate_date    date,
    run_type      text,                        -- scheduled_* | manual
    stage         text,                        -- collection | processing
    status        text,                        -- ok | partial | failed
    detail        text,
    started_at    timestamptz,
    finished_at   timestamptz default now()
);
create index if not exists pipeline_runs_date_idx on pipeline_runs (slate_date, finished_at desc);

-- ============================================================ security
-- RLS is on for every table. The pipeline writes with the service key, which
-- bypasses RLS. The dashboard reads with the publishable key, so each table needs an
-- explicit read policy — nothing is exposed by accident.
--
-- If "Automatically expose new tables" is disabled on the project (the safer setting),
-- new tables get NO privileges, not even for service_role, and every write fails with
-- "42501 permission denied". The grants below fix that either way.

do $$
declare t text;
begin
  foreach t in array array['players','games','goalie_starts','player_status',
                           'prop_picks','game_picks','team_stats','market_lines',
                           'pipeline_runs']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('grant select, insert, update, delete on %I to service_role', t);
    execute format('grant select on %I to anon, authenticated', t);
    execute format('drop policy if exists "public read" on %I', t);
    execute format('create policy "public read" on %I for select to anon, authenticated using (true)', t);
  end loop;
end $$;

-- pipeline_runs.id is a bigserial, so its sequence needs granting too
grant usage, select on all sequences in schema public to service_role;

-- ============================================================ results / track record
-- Same as db/results.sql (order 3, Oct 2026).

create table if not exists pick_snapshots (
    slate_date    date not null,
    board         text not null,              -- sog | points | goals | assists | ppp |
                                              -- total_goals | team_goals | moneyline | puck_line
    game_id       bigint not null,
    subject       text not null,              -- player_id, team abbrev, or the picked side
    subject_name  text,
    team          text,
    opponent      text,
    rank          int,                        -- rank on the board in the run it was frozen from
    score         numeric,                    -- 0-100 percentile within that run's slate
    line          numeric,                    -- the number it is graded against, if any
    extra         jsonb,                      -- lean / tag / sides / market favourite
    game_start    timestamptz not null,
    run_type      text,
    frozen_at     timestamptz not null,
    primary key (slate_date, board, game_id, subject)
);
create index if not exists pick_snapshots_game_idx on pick_snapshots (slate_date, game_id);

create table if not exists pick_results (
    slate_date    date not null,
    board         text not null,              -- graded board, e.g. goals | multi_goal | sog_safe
    game_id       bigint not null,
    subject       text not null,
    subject_name  text,
    team          text,
    opponent      text,
    rank          int,                        -- rank among the frozen picks of that board
    frozen_rank   int,
    score         numeric,
    pick          text,                       -- what was graded, e.g. "1+ goal", "Over 6.5"
    line          numeric,
    actual        numeric,
    actual_text   text,
    result        text not null,              -- hit | miss | void
    note          text,                       -- why void, OT/SO detail
    base_p        numeric,                    -- base rate of this board that day (0-1)
    frozen_at     timestamptz,
    run_type      text,
    graded_at     timestamptz default now(),
    primary key (slate_date, board, game_id, subject)
);
create index if not exists pick_results_date_idx on pick_results (slate_date);

-- A started game's snapshot is frozen: no insert, update or delete once its start time
-- has passed. Old rows (graded long ago) may be pruned.
create or replace function pick_snapshots_frozen() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    if old.game_start <= now() and old.slate_date >= current_date - 14 then
      raise exception 'pick_snapshots: game % started at %, its picks are frozen',
        old.game_id, old.game_start;
    end if;
    return old;
  end if;
  if new.game_start <= now() then
    raise exception 'pick_snapshots: game % started at %, its picks are frozen',
      new.game_id, new.game_start;
  end if;
  if tg_op = 'UPDATE' and old.game_start <= now() then
    raise exception 'pick_snapshots: game % already started', old.game_id;
  end if;
  return new;
end $$;

drop trigger if exists pick_snapshots_frozen on pick_snapshots;
create trigger pick_snapshots_frozen
  before insert or update or delete on pick_snapshots
  for each row execute function pick_snapshots_frozen();

do $$
declare t text;
begin
  foreach t in array array['pick_snapshots','pick_results']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('grant select, insert, update, delete on %I to service_role', t);
    execute format('grant select on %I to anon, authenticated', t);
    execute format('drop policy if exists "public read" on %I', t);
    execute format('create policy "public read" on %I for select to anon, authenticated using (true)', t);
  end loop;
end $$;
