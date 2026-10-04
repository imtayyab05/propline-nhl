"""Daily collection — everything the scoring step needs, from the official NHL feed.

  schedule + current rosters
  + game-by-game logs for every skater, goalie and team (this season and last)
  + tonight's game rosters / confirmed starters, when the league has posted them
  + projected starting goalies and who is out (with the minutes they leave behind)
  -> data/raw/{day}/logs/*.csv   (the big game logs, read directly by processing)
  -> data/intermediate/collection_{day}.xlsx   (everything else, one sheet each)

Last season's logs never change, so they are cached under data/cache/{season}/ and
pulled once. This season's are pulled fresh every run.

Examples
--------
  python scripts/collect.py                    # today
  python scripts/collect.py --date 2026-10-02
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline.availability import availability, team_absences  # noqa: E402
from propline.db import load_env, log_run  # noqa: E402
from propline.goalies import project_starters  # noqa: E402
from propline.intermediate import build_intermediate  # noqa: E402
from propline import nhl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="PropLine NHL — daily collection")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--run-kind", default="manual")
    ap.add_argument("--refresh-cache", action="store_true",
                    help="re-pull last season's logs even if they are cached")
    args = ap.parse_args()
    load_env()

    started = datetime.now()
    day = args.date
    today = nhl.parse_date(day)
    season = nhl.season_of(today)
    prev = nhl.previous_season(season)
    raw_dir = Path("data/raw") / day
    logs_dir = raw_dir / "logs"
    cache = Path("data/cache") / str(prev)
    out_xlsx = Path("data/intermediate") / f"collection_{day}.xlsx"
    logs_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*66}\nPropLine NHL collection — {day} (season {season})\n{'='*66}")

    # 1. Teams and tonight's slate
    print("\n[1/6] Teams and schedule")
    teams = nhl.get_teams()
    schedule = nhl.get_schedule(day)
    print(f"  ok    schedule: {len(schedule)} games")
    if schedule.empty:
        print("  ok    no regular-season or playoff games today")

    # 2. Team and goalie game logs — small, one request per report per season
    print("\n[2/6] Team and goalie game logs")
    team_prev = nhl.cached(cache / "team_logs.csv",
                           lambda: nhl.team_game_logs(prev, teams), args.refresh_cache)
    team_cur = nhl.team_game_logs(season, teams)
    team_logs = pd.concat([team_prev, team_cur], ignore_index=True)
    team_logs = team_logs[team_logs["game_date"] < day]
    unmapped = team_logs["team"].isna().sum()
    if unmapped:
        # every downstream join is on the abbreviation — a silent miss here would drop
        # a whole team from the boards without an error
        raise nhl.FeedError(f"{unmapped} team-log rows have no abbreviation for their "
                            f"team id — the stats API team list has changed")
    print(f"  ok    team logs: {len(team_prev)} last season, {len(team_cur)} this season")

    goalie_prev = nhl.cached(cache / "goalie_logs.csv",
                             lambda: nhl.goalie_game_logs(prev), args.refresh_cache)
    goalie_cur = nhl.goalie_game_logs(season)
    goalie_logs = pd.concat([goalie_prev, goalie_cur], ignore_index=True)
    goalie_logs = goalie_logs[goalie_logs["game_date"] < day]
    print(f"  ok    goalie logs: {len(goalie_prev)} last season, {len(goalie_cur)} this season")

    # 3. Skater game logs. Last season in full (cached), this season to yesterday.
    print("\n[3/6] Skater game logs")
    p_start, p_end = nhl.season_bounds(team_prev, prev)
    if p_start is None:
        raise nhl.FeedError(f"no regular-season team logs for {prev} — cannot bound "
                            f"last season's skater pull")
    skater_prev = nhl.cached(
        cache / "skater_logs.csv",
        lambda: nhl.skater_game_logs(prev, nhl.parse_date(p_start),
                                     nhl.parse_date(p_end), include_playoffs=False),
        args.refresh_cache)
    c_start, _ = nhl.season_bounds(team_cur, season)
    yesterday = today - timedelta(days=1)
    if c_start and nhl.parse_date(c_start) <= yesterday:
        skater_cur = nhl.skater_game_logs(season, nhl.parse_date(c_start), yesterday)
    else:
        skater_cur = pd.DataFrame(columns=nhl.SKATER_LOG_COLS)
    skater_logs = pd.concat([skater_prev, skater_cur], ignore_index=True)
    print(f"  ok    skater logs: {len(skater_prev)} last season, "
          f"{len(skater_cur)} this season ({skater_cur['game_id'].nunique()} games)")

    # Coverage check: every completed game this season should have skater rows. A gap
    # means the stats API is lagging or a chunk failed — say so, do not score on it.
    done_games = set(team_cur.loc[team_cur["game_date"] < day, "game_id"])
    missing = done_games - set(skater_cur["game_id"])
    if missing:
        print(f"  WARN  {len(missing)} completed game(s) have no skater rows yet: "
              f"{sorted(missing)[:5]}")

    # 4. Rosters for tonight's teams
    print("\n[4/6] Current rosters")
    playing = sorted(set(schedule["home_team"]) | set(schedule["away_team"]))
    rosters = nhl.get_rosters(playing)
    print(f"  ok    {len(rosters)} players on {rosters['team'].nunique()} rosters")

    # 5. What the league has published for tonight
    print("\n[5/6] Game rosters, starters and who is out")
    dressed, confirmed = nhl.get_game_day(schedule["game_id"])
    print(f"  ok    game rosters posted for {dressed['game_id'].nunique()}/{len(schedule)} "
          f"games, starters confirmed for {len(confirmed)} teams")

    starters = project_starters(schedule, rosters, goalie_logs, team_logs, season, prev,
                                day, dressed=dressed, confirmed=confirmed)
    for _, s in starters.iterrows():
        print(f"        {s['team']:4} {str(s['goalie_name']):24} {s['status']:9} "
              f"{s['confidence']:9} {s['reason']}")

    avail = availability(schedule, rosters, skater_logs, team_logs, season, day,
                         dressed=dressed)
    absences = team_absences(avail)
    outs = avail[(avail["status"] == "out") & avail["regular"]] if not avail.empty else avail
    print(f"  ok    {len(outs)} regulars out, "
          f"{int((avail.get('toi_bump', pd.Series(dtype=float)) > 0).sum())} teammates bumped")
    for _, o in outs.iterrows():
        print(f"        OUT {o['team']:4} {o['player_name']:24} "
              f"{o['base_toi']:.1f} min  {o['reason']}")

    # 6. Write
    print("\n[6/6] Writing")
    for name, df in (("team_logs", team_logs), ("goalie_logs", goalie_logs),
                     ("skater_logs", skater_logs)):
        df.to_csv(logs_dir / f"{name}.csv", index=False)
    meta = pd.DataFrame([{"day": day, "season": season, "prev_season": prev,
                          "collected_at": datetime.now().isoformat(timespec="seconds"),
                          "games": len(schedule),
                          "games_with_roster": int(dressed["game_id"].nunique()),
                          "missing_skater_games": len(missing)}])
    extra = {"meta": meta, "schedule": schedule, "teams": teams, "rosters": rosters,
             "goalie_starts": starters, "availability": avail,
             "team_absences": absences, "dressed": dressed}
    sheets = build_intermediate(raw_dir, out_xlsx, extra=extra)
    print(f"  ok    {out_xlsx}  ({len(sheets)} sheets)")

    status = "partial" if missing else "ok"
    log_run(day, args.run_kind, "collection", status,
            detail=(f"{len(schedule)} games, {len(skater_cur)} skater rows this season, "
                    f"{len(outs)} regulars out, goalies: "
                    f"{(starters['status'] == 'confirmed').sum()} confirmed / {len(starters)}"),
            started_at=started)
    print(f"\n{'='*66}\ncollection {status.upper()}")
    return 0


def _log_crash(stage: str) -> None:
    """A crash before the normal log_run never reached pipeline_runs, so GitHub went red
    while the dashboard still read "ok" (MLB lesson). Record it here instead."""
    try:
        args = sys.argv
        day = args[args.index("--date") + 1] if "--date" in args else date.today().isoformat()
        kind = args[args.index("--run-kind") + 1] if "--run-kind" in args else "manual"
        load_env()
        log_run(day, kind, stage, "failed",
                detail=traceback.format_exc().strip().splitlines()[-1][:400])
    except Exception:  # noqa: BLE001 — never mask the original error
        pass


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except Exception:
        traceback.print_exc()
        _log_crash("collection")
        raise SystemExit(1)
