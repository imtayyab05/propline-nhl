-- PropLine NHL - manual game totals (Nov 2026). Run once in the Supabase SQL editor.
-- Safe to re-run. Also appended to schema.sql.
--
-- Devin's book can post a different total from our US consensus line, which changes the
-- model lean he sees. One active manual total per game, today's slate only, written only
-- by /api/override (ADMIN_SECRET + service key). A separate table because a total has a
-- number and optional prices, not a player and a status.
--
-- Same audit rules as manual_overrides: rows are never deleted ("remove" sets
-- removed_at) and nothing can change once the game has started. The anon key has no
-- access at all; the boards carry the label, so nobody needs this table directly.
--
-- A manual total changes what is DISPLAYED (Market Total, Model Lean, Proj - Market, the
-- AI feed). It never changes a score, and the Results page keeps grading the lean from
-- the consensus line (propline/results.py).

create table if not exists manual_totals (
    id           bigserial primary key,
    slate_date   date not null,
    game_id      bigint not null,
    matchup      text,
    total        numeric not null check (total between 4.5 and 9.5 and total * 2 = floor(total * 2)),
    over_price   int check (over_price is null or abs(over_price) between 100 and 2000),
    under_price  int check (under_price is null or abs(under_price) between 100 and 2000),
    game_start   timestamptz not null,
    set_at       timestamptz not null default now(),
    removed_at   timestamptz
);
create unique index if not exists manual_totals_active_uq
    on manual_totals (slate_date, game_id) where removed_at is null;

create or replace function manual_totals_before_start() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'manual_totals: rows are never deleted; set removed_at';
  end if;
  if new.game_start <= now() then
    raise exception 'manual_totals: game % started at %, too late to change',
      new.game_id, new.game_start;
  end if;
  return new;
end $$;

drop trigger if exists manual_totals_before_start on manual_totals;
create trigger manual_totals_before_start
  before insert or update or delete on manual_totals
  for each row execute function manual_totals_before_start();

alter table manual_totals enable row level security;
grant select, insert, update, delete on manual_totals to service_role;
revoke all on manual_totals from anon, authenticated;
grant usage, select on all sequences in schema public to service_role;
