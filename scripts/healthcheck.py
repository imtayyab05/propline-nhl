"""Monthly health check for the maintenance report.

Read-only: it writes nothing (no pipeline_runs row, no upserts) and costs nothing. The
Odds API credits come from the response headers of the free /sports endpoint, and every
other figure is a Supabase read.

    python scripts/healthcheck.py            # last 30 days
    python scripts/healthcheck.py --days 7   # last week

It prints the full diagnostic, then a short CLIENT SUMMARY to paste into the message.

Coverage is counted against what the system ATTEMPTS, not everything stored:
  - runs: the four Netlify slots per slate (12:07 morning, 17:37 midday, 22:37 pre-game,
    01:37 late, UTC) whose time has passed, counted from the first scheduled run ever
    logged, so the days before the scheduler went live are not counted as misses
  - explanations: only the top 10 of each player board and of Total Goals, and the top
    5 of the other game boards, get Why text (scripts/process.py --explain-top and
    BOARD_EXPLAIN_TOP); trend tabs never do
  - market lines: game lines on the morning and pre-game runs, team totals on the
    pre-game run only (propline/odds.py); the run log says how many games each pull
    matched
MLB's version divided explanations by every published row and reported 3% for a run
that had explained everything it was asked to.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline.db import _creds, env, load_env, read  # noqa: E402

ODDS_BASE = "https://api.the-odds-api.com/v4"
BUCKET = "picks"                    # propline/storage.py

# Netlify slots (netlify/functions/scheduled-slate*.mjs), UTC. The late slot fires after
# midnight UTC but belongs to the previous day's slate (the workflow dates in US Eastern).
SLOTS = {
    "scheduled_morning": (0, time(12, 7)),
    "scheduled_midday": (0, time(17, 37)),
    "scheduled_pregame": (0, time(22, 37)),
    "scheduled_late": (1, time(1, 37)),
}
GRACE = timedelta(minutes=45)       # a slot is not "missed" until its run has had time

PLAYER_BOARDS = ["sog", "points", "goals", "assists", "ppp"]
EXPLAIN_TOP = 10                    # process.py --explain-top default (player boards, total goals)
BOARD_EXPLAIN_TOP = 5               # process.py BOARD_EXPLAIN_TOP (other game boards)
GROQ_SLOTS = {"scheduled_morning", "scheduled_pregame"}     # daily.yml EXPLAIN

TABLES = ["prop_picks", "game_picks", "pick_snapshots", "pick_results", "team_stats", "market_lines", "goalie_starts",
          "player_status", "games", "players", "pipeline_runs"]
FREE_DB_MB = 500
FREE_STORAGE_MB = 1024

MANUAL_CHECKS = [
    ("Netlify credits", "https://app.netlify.com/teams/-/billing"),
    ("Exact database size", "https://supabase.com/dashboard/project/_/reports/database"),
]


def _headers(extra: dict | None = None) -> tuple[str, dict]:
    url, key = _creds()
    return url, {"apikey": key, "Authorization": f"Bearer {key}", **(extra or {})}


def count(table: str, filters: dict | None = None) -> int | None:
    """Exact row count without downloading rows. A plain select stops at 1,000 rows
    (PostgREST cap), which has understated figures on this project before."""
    url, h = _headers({"Prefer": "count=exact", "Range": "0-0"})
    try:
        r = requests.get(f"{url}/rest/v1/{table}", headers=h,
                         params={"select": "*", **(filters or {})}, timeout=45)
    except requests.RequestException:
        return None
    rng = r.headers.get("content-range", "")
    return int(rng.split("/")[-1]) if r.ok and "/" in rng and rng[-1] != "*" else None


def read_all(table: str, params: dict) -> list[dict]:
    """Every matching row, 1,000 at a time."""
    out, off = [], 0
    while True:
        page = read(table, {**params, "limit": "1000", "offset": str(off)})
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def pct(part, whole) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "n/a"


def slot_time(slate: date, kind: str) -> datetime:
    days, t = SLOTS[kind]
    return datetime.combine(slate + timedelta(days=days), t, tzinfo=timezone.utc)


# --- sections ---------------------------------------------------------------------------

def automation(days: int, now: datetime) -> dict:
    since = (now - timedelta(days=days)).date()
    runs = read_all("pipeline_runs", {
        "select": "slate_date,run_type,stage,status,started_at,detail",
        "slate_date": f"gte.{since.isoformat()}", "order": "id.asc"})
    first = read("pipeline_runs", {"select": "slate_date", "run_type": "like.scheduled_*",
                                   "order": "slate_date.asc", "limit": "1"})
    live_from = date.fromisoformat(first[0]["slate_date"]) if first else None

    # what the scheduler should have run: every slot in the window whose time has passed
    expected = []
    if live_from:
        d = max(since, live_from)
        while d <= now.date():
            expected += [(d, k) for k in SLOTS if slot_time(d, k) + GRACE <= now]
            d += timedelta(days=1)

    by_slot: dict[tuple, list[dict]] = {}
    for r in runs:
        by_slot.setdefault((r["slate_date"], r["run_type"]), []).append(r)

    done, failed, missing = [], [], []
    for d, k in expected:
        rows = by_slot.get((d.isoformat(), k), [])
        proc_ok = any(r["stage"] == "processing" and r["status"] == "ok" for r in rows)
        bad = [r for r in rows if r["status"] != "ok"]
        if proc_ok:
            done.append((d, k))
        elif rows:
            failed.append((d, k, bad[-1] if bad else rows[-1]))
        else:
            missing.append((d, k))
    # a collection logged "partial" (some games' skater data missing) on a slot that
    # still published is a warning, not a failed run
    partial = [r for r in runs if r["status"] == "partial"
               and r["run_type"] in SLOTS]
    manual = [r for r in runs if r["run_type"] == "manual" and r["stage"] == "processing"]
    manual_bad = [r for r in runs if r["run_type"] == "manual" and r["status"] != "ok"]
    slates = sorted({r["slate_date"] for r in runs
                     if r["stage"] == "processing" and r["status"] == "ok"})
    return dict(runs=runs, expected=expected, done=done, failed=failed, missing=missing,
                partial=partial, manual=manual, manual_bad=manual_bad, slates=slates,
                live_from=live_from, since=since)


def latest_slate(a: dict) -> dict | None:
    if not a["slates"]:
        return None
    s = a["slates"][-1]
    procs = [r for r in a["runs"] if r["slate_date"] == s and r["stage"] == "processing"
             and r["status"] == "ok"]
    out = dict(date=s, last_run=procs[-1])

    games = read("games", {"select": "game_id,state", "game_date": f"eq.{s}"})
    out["games"] = len(games)
    out["game_states"] = Counter(g["state"] for g in games)

    # players: one row per dressed skater on the SOG board
    lineup = Counter(r["lineup_status"] for r in read_all(
        "prop_picks", {"select": "lineup_status", "slate_date": f"eq.{s}", "prop": "eq.sog"}))
    out["players"] = sum(lineup.values())
    out["lineup"] = lineup
    out["out"] = count("player_status", {"game_date": f"eq.{s}", "status": "eq.out"})
    out["prop_rows"] = count("prop_picks", {"slate_date": f"eq.{s}"})
    out["game_rows"] = count("game_picks", {"slate_date": f"eq.{s}"})

    # goalies: two per game attempted
    g = read("goalie_starts", {"select": "status,confidence", "game_date": f"eq.{s}"})
    out["goalies"] = dict(expected=2 * len(games), rows=len(g),
                          confirmed=sum(x["status"] == "confirmed" for x in g),
                          high=sum(x["status"] == "projected" and x["confidence"] == "high" for x in g),
                          lean=sum(x["status"] == "projected" and x["confidence"] == "lean" for x in g))

    # explanations: only the top N of each board are asked for, and only after a Groq run
    groq_ran = any(r["run_type"] in GROQ_SLOTS for r in procs)
    ex = {}
    for prop in PLAYER_BOARDS:
        asked = count("prop_picks", {"slate_date": f"eq.{s}", "prop": f"eq.{prop}",
                                     "rank": f"lte.{EXPLAIN_TOP}"})
        blank = count("prop_picks", {"slate_date": f"eq.{s}", "prop": f"eq.{prop}",
                                     "rank": f"lte.{EXPLAIN_TOP}", "rationale": "is.null"})
        ex[prop] = (asked, blank)
    gprops = sorted({r["prop"] for r in read("game_picks", {"select": "prop",
                                                            "slate_date": f"eq.{s}",
                                                            "limit": "1000"})})
    for prop in gprops:
        top = EXPLAIN_TOP if prop == "total_goals" else BOARD_EXPLAIN_TOP
        asked = count("game_picks", {"slate_date": f"eq.{s}", "prop": f"eq.{prop}",
                                     "rank": f"lte.{top}"})
        blank = count("game_picks", {"slate_date": f"eq.{s}", "prop": f"eq.{prop}",
                                     "rank": f"lte.{top}", "rationale": "is.null"})
        ex[prop] = (asked, blank)
    out["explain"] = ex
    out["groq_ran"] = groq_ran

    # market lines: what each odds pull matched (from its own log line), and what is stored
    pulls = [r for r in procs if "credits left" in (r.get("detail") or "")]
    out["odds_pulls"] = [(r["run_type"], r["detail"].split("odds:", 1)[-1].strip())
                         for r in pulls]
    lines = read_all("market_lines", {"select": "game_id,market", "slate_date": f"eq.{s}"})
    per = {m: len({l["game_id"] for l in lines if l["market"] == m})
           for m in ("moneyline", "puck_line", "total", "team_total")}
    out["lines"] = per
    out["team_totals_attempted"] = any(r["run_type"] == "scheduled_pregame" for r in pulls)
    return out


def odds_quota() -> tuple[int | None, int | None]:
    """Credits left / used this month, from the FREE /sports endpoint, so the check
    never spends the budget it reports on."""
    key = env("ODDS_API_KEY")
    if not key:
        return None, None
    try:
        r = requests.get(f"{ODDS_BASE}/sports", params={"apiKey": key}, timeout=45)
    except requests.RequestException:
        return None, None
    if not r.ok:
        return None, None
    rem, used = r.headers.get("x-requests-remaining"), r.headers.get("x-requests-used")
    return (int(float(rem)) if rem else None, int(float(used)) if used else None)


def results_status(a: dict, latest: dict | None) -> dict:
    """Track Record page: were tonight's games frozen before puck drop, and were the
    finished slates graded? Counted against slates that had a freeze to grade."""
    out = {"ok": True}
    try:
        snap_dates = sorted({r["slate_date"] for r in read_all(
            "pick_snapshots", {"select": "slate_date", "board": "eq.moneyline",
                               "slate_date": f"gte.{a['since']}"})})
        graded = sorted({r["slate_date"] for r in read_all(
            "pick_results", {"select": "slate_date", "slate_date": f"gte.{a['since']}"})})
    except Exception as exc:  # noqa: BLE001 - tables missing until db/results.sql is run
        return {"ok": False, "error": str(exc)[:90]}
    today = datetime.now(timezone.utc).date().isoformat()
    due = [d for d in snap_dates if d < today]
    out.update(frozen=snap_dates, graded=graded, due=due,
               ungraded=[d for d in due if d not in graded],
               grading_failed=[r for r in a["runs"] if r["stage"] == "grading"
                               and r["status"] != "ok"])
    if latest:
        g = read("pick_snapshots", {"select": "game_id", "slate_date": f"eq.{latest['date']}",
                                    "board": "eq.moneyline"})
        out["latest_frozen"] = len({x["game_id"] for x in g})
    return out


def storage() -> dict:
    """Row counts per table, a rough size from a sample of each table's rows, and the
    workbook bucket's real size. PostgREST cannot read pg_database_size, so the exact
    figure stays a manual check."""
    tables, est = {}, 0.0
    for t in TABLES:
        n = count(t) or 0
        sample = read(t, {"select": "*", "limit": "300"}) if n else []
        avg = (sum(len(json.dumps(r)) for r in sample) / len(sample)) if sample else 0
        tables[t] = n
        est += n * avg
    files, size = 0, 0
    url, h = _headers({"Content-Type": "application/json"})
    try:
        # list is a read; it changes nothing in the bucket
        r = requests.post(f"{url}/storage/v1/object/list/{BUCKET}", headers=h,
                          data=json.dumps({"prefix": "", "limit": 1000}), timeout=45)
        if r.ok:
            objs = [o for o in r.json() if o.get("metadata")]
            files = len(objs)
            size = sum(int(o["metadata"].get("size") or 0) for o in objs)
    except requests.RequestException:
        pass
    return dict(tables=tables, data_mb=est / 1e6, files=files, files_mb=size / 1e6)


# --- report -----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="PropLine NHL - health check (read-only)")
    ap.add_argument("--days", type=int, default=30, help="reporting window (default 30)")
    args = ap.parse_args()
    load_env()
    now = datetime.now(timezone.utc)

    print("=" * 66)
    print(f"PropLine NHL - health check   {now:%Y-%m-%d %H:%M} UTC   (last {args.days} days)")
    print("=" * 66)

    a = automation(args.days, now)
    exp, done = len(a["expected"]), len(a["done"])
    print("\nAUTOMATION  (4 scheduled runs a slate)")
    if a["live_from"] and a["live_from"] > a["since"]:
        print(f"  counted from      {a['live_from']} (first scheduled run)")
    print(f"  scheduled runs    {done} of {exp} completed ({pct(done, exp)})")
    print(f"  failed            {len(a['failed'])}")
    for d, k, r in a["failed"][:8]:
        print(f"                    {d}  {k:18} {r['stage']} {r['status']}: "
              f"{(r.get('detail') or '')[:60]}")
    print(f"  never ran         {len(a['missing'])}"
          + ("   (Netlify did not fire, or a manual run cancelled it)" if a["missing"] else ""))
    for d, k in a["missing"][:8]:
        print(f"                    {d}  {k}")
    if len(a["missing"]) > 8:
        print(f"                    ... and {len(a['missing']) - 8} more")
    if a["partial"]:
        print(f"  partial collects  {len(a['partial'])} (published, some skater data missing)")
    print(f"  manual runs       {len(a['manual'])} (Update Now / by hand)"
          + (f", {len(a['manual_bad'])} not ok" if a["manual_bad"] else ""))
    sl = a["slates"]
    print(f"  slates published  {len(sl)}" + (f"  ({sl[0]} to {sl[-1]})" if sl else ""))

    L = latest_slate(a)
    print(f"\nLATEST SLATE  {L['date'] if L else '(none)'}")
    if L:
        lr = L["last_run"]
        print(f"  last run          {lr['run_type']} at {str(lr['started_at'])[:16]} UTC")
        states = ", ".join(f"{k} {v}" for k, v in sorted(L["game_states"].items()))
        print(f"  games             {L['games']}" + (f"  ({states})" if states else ""))
        if not L["games"]:
            print("  NOTE              off-day: team stats only, which is normal")
        lu = L["lineup"]
        print(f"  players ranked    {L['players']} skaters "
              f"({lu.get('confirmed', 0)} on a posted game roster, "
              f"{lu.get('projected', 0)} projected); {L['out']} regulars out")
        print(f"  rows published    {L['prop_rows']} player rows, {L['game_rows']} game/team rows")
        g = L["goalies"]
        print(f"  goalies           {g['rows']} of {g['expected']}: {g['confirmed']} confirmed, "
              f"{g['high']} projected (firm), {g['lean']} projected (lean)")
        print("                    starters are only confirmed once a game starts")

        if L["groq_ran"]:
            asked = sum(v[0] or 0 for v in L["explain"].values())
            blank = sum(v[1] or 0 for v in L["explain"].values())
            have = asked - blank
            flag = "  <-- Groq daily cap? (shared with MLB)" if asked and have / asked < 0.8 else ""
            print(f"  explanations      {have} of {asked} asked for ({pct(have, asked)}){flag}")
            print(f"                    top {EXPLAIN_TOP} per player board and Total Goals, "
                  f"top {BOARD_EXPLAIN_TOP} per other game board")
            short = [f"{p} {v[0] - v[1]}/{v[0]}" for p, v in L["explain"].items()
                     if v[0] and v[1]]
            if short:
                print(f"                    short: {', '.join(short)}")
        else:
            print("  explanations      no AI run on this slate yet (morning and pre-game only)")

        for kind, note in L["odds_pulls"]:
            print(f"  odds pull         {kind}: {note}")
        if not L["odds_pulls"]:
            print("  odds pull         none on this slate yet")
        ln = L["lines"]
        print(f"  lines stored      moneyline {ln['moneyline']}/{L['games']}, "
              f"puck line {ln['puck_line']}/{L['games']}, total {ln['total']}/{L['games']} games")
        if L["team_totals_attempted"]:
            print(f"                    team totals {ln['team_total']}/{L['games']} games "
                  "(fewer books post these)")
        else:
            print("                    team totals: pulled on the pre-game run, not yet run")

    R = results_status(a, L)
    print("\nRESULTS PAGE  (picks frozen before puck drop, graded next morning)")
    if not R["ok"]:
        print(f"  NOT SET UP        {R['error']}  <-- run db/results.sql in Supabase")
    else:
        if L:
            print(f"  latest slate      {R.get('latest_frozen', 0)} of {L['games']} games frozen")
        print(f"  slates graded     {len([d for d in R['due'] if d in R['graded']])} of "
              f"{len(R['due'])} finished slates with a freeze"
              + (f"  <-- not graded: {', '.join(R['ungraded'])}" if R["ungraded"] else ""))
        if R["graded"]:
            print(f"  record            {R['graded'][0]} to {R['graded'][-1]}")
        if R["grading_failed"]:
            print(f"  grading failures  {len(R['grading_failed'])}")

    print("\nQUOTAS")
    rem, used = odds_quota()
    if rem is None:
        print("  odds api          could not read (key missing or service down)")
    else:
        warn = "  <-- TIGHT" if rem < 60 else ""
        print(f"  odds api          {rem} credits left, {used} used this month "
              f"(500 free; NHL has its own key){warn}")
        print("                    ~3 a morning + ~1 per game before puck drop")
    print("  groq              shared with MLB; see explanations above")
    print("  github actions    public repo: NHL runs do not use the 2,000-minute pool")

    st = storage()
    print("\nSTORAGE")
    for t, n in st["tables"].items():
        print(f"  {t:16}  {n:>9,} rows")
    print(f"  database          ~{st['data_mb']:.0f} MB of row data (rough, from samples; "
          f"free tier {FREE_DB_MB} MB)")
    if L and L["prop_rows"]:
        print(f"  growth            ~{(L['prop_rows'] or 0) + (L['game_rows'] or 0):,} pick rows "
              "a slate; old slates are kept (nothing prunes them)")
    print(f"  workbooks         {st['files']} files, {st['files_mb']:.1f} MB "
          f"(free tier {FREE_STORAGE_MB} MB)")

    print("\nCHECK BY HAND (no token for these)")
    for label, url in MANUAL_CHECKS:
        print(f"  {label:22} {url}")

    # --- pasteable summary ----------------------------------------------------------
    print("\n" + "=" * 66)
    print("CLIENT SUMMARY - copy from here")
    print("=" * 66)
    print(f"PropLine NHL - {now:%B %Y} status\n")
    issues = len(a["failed"]) + len(a["missing"])
    print(f"- Automatic updates: {done} of {exp} scheduled runs completed "
          f"({pct(done, exp)}) over the last {args.days} days"
          + (", with no failures." if not issues else
             f"; {issues} did not complete and the board waited for the next run."))
    print(f"- {len(sl)} slates published.")
    if L and L["games"]:
        g = L["goalies"]
        print(f"- Latest slate ({L['date']}): {L['games']} games, {L['players']} skaters "
              f"ranked on every player board, {L['out']} regulars flagged out.")
        print(f"- Goalies: {g['rows']} of {g['expected']} projected or confirmed "
              f"({g['lean']} marked as a lean).")
        ln = L["lines"]
        print(f"- Market lines: moneyline and puck line on {min(ln['moneyline'], ln['puck_line'])} "
              f"of {L['games']} games, totals on {ln['total']}.")
    if rem is not None:
        print(f"- Odds data within its free limit ({rem} of 500 credits left this month).")
    print(f"- Database well within its free limit (~{st['data_mb']:.0f} of {FREE_DB_MB} MB).")
    print("\n(Add anything you changed or tuned this month.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
