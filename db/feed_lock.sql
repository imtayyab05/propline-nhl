-- PropLine NHL - close the AI feed table to the public key (4 Nov 2026).
-- Run once in the Supabase SQL editor, AFTER the site deploy that reads ai_feed with the
-- service key (netlify/functions/feed.mjs). Safe to re-run.
--
-- The feed is now passphrase-gated (FEED_SECRET). Without this, anyone with the public
-- anon key (it ships in the dashboard) could still read ai_feed straight from Supabase.
-- The pipeline writes it with the service key, which RLS does not restrict.

drop policy if exists "public read" on ai_feed;
revoke select, insert, update, delete, truncate on ai_feed from anon, authenticated;
alter table ai_feed enable row level security;
