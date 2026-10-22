-- PropLine NHL - Results / Track Record (order 3, Oct 2026).
-- Run once in the Supabase SQL editor. Safe to re-run. Also appended to schema.sql.
--
-- pick_snapshots: each game's picks as they stood at the LAST run before its puck
-- drop. The boards themselves (prop_picks / game_picks) are replaced on every run, and
-- the late run (01:37 UTC) fires after the early games have started and re-scores them
-- with post-start information (boxscore-confirmed goalies, posted rosters). Grading
-- those boards would grade picks nobody could have seen before the game. So every
-- publish rewrites the snapshot rows of games that have NOT started, and never touches
-- a started game's rows. The trigger below enforces that in the database as well.
--
-- pick_results: one graded row per pick (top 25 of each player board, every row of
-- the game boards that are graded), with the base rate of its board that day.

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
