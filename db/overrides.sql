-- PropLine NHL - Manual overrides + AI data feed (order 4, Nov 2026).
-- Run once in the Supabase SQL editor. Safe to re-run. Also appended to schema.sql.

-- ============================================================ manual overrides
-- Devin's corrections for TODAY's slate: confirm a team's starting goalie, or mark a
-- skater OUT / IN. Written only by the Netlify function /api/override with the service
-- key after the admin passphrase (ADMIN_SECRET) checks out. The anon key can read
-- (the boards label overridden rows) but never write: no insert/update/delete grant
-- and no write policy for anon / authenticated.
--
-- Rows are never deleted: "remove" sets removed_at, so the record of what was changed
-- and when survives for the health check and the Results page. Priority in the
-- pipeline: official final game roster > manual override > projection.

create table if not exists manual_overrides (
    id           bigserial primary key,
    slate_date   date not null,
    kind         text not null check (kind in ('goalie', 'status')),
    team         text not null,
    player_id    bigint not null,
    player_name  text,
    value        text not null check (value in ('confirmed', 'out', 'in')),
    game_id      bigint not null,
    game_start   timestamptz not null,
    set_at       timestamptz not null default now(),
    removed_at   timestamptz
);
-- one active goalie per team and one active status per player, per slate
create unique index if not exists manual_overrides_goalie_uq
    on manual_overrides (slate_date, team) where kind = 'goalie' and removed_at is null;
create unique index if not exists manual_overrides_status_uq
    on manual_overrides (slate_date, player_id) where kind = 'status' and removed_at is null;

-- No override (and no removal) once the game has started: the Results page grades the
-- board as it stood at puck drop, so nothing may change it afterwards.
create or replace function manual_overrides_before_start() returns trigger
language plpgsql as $$
begin
  if tg_op = 'DELETE' then
    raise exception 'manual_overrides: rows are never deleted; set removed_at';
  end if;
  if new.game_start <= now() then
    raise exception 'manual_overrides: game % started at %, too late to change',
      new.game_id, new.game_start;
  end if;
  return new;
end $$;

drop trigger if exists manual_overrides_before_start on manual_overrides;
create trigger manual_overrides_before_start
  before insert or update or delete on manual_overrides
  for each row execute function manual_overrides_before_start();

-- ============================================================ AI data feed
-- One compact summary per slate, rebuilt by every publish; served read-only at
-- /api/feed (JSON or Markdown) with the anon key. Nothing private in it.
create table if not exists ai_feed (
    slate_date    date primary key,
    feed          jsonb not null,
    run_type      text,
    generated_at  timestamptz not null default now()
);

-- ============================================================ results: overrides
-- A graded pick whose board was touched by an override says so.
alter table pick_results add column if not exists override text;

-- ============================================================ security
do $$
declare t text;
begin
  foreach t in array array['manual_overrides', 'ai_feed']
  loop
    execute format('alter table %I enable row level security', t);
    execute format('grant select, insert, update, delete on %I to service_role', t);
    execute format('revoke insert, update, delete, truncate on %I from anon, authenticated', t);
    execute format('grant select on %I to anon, authenticated', t);
    execute format('drop policy if exists "public read" on %I', t);
    execute format('create policy "public read" on %I for select to anon, authenticated using (true)', t);
  end loop;
end $$;

grant usage, select on all sequences in schema public to service_role;
