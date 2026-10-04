"""Turn a collection run into ranked props, total goals and the team stats tab.

Reads data/intermediate/collection_{day}.xlsx and data/raw/{day}/logs/*.csv from
scripts/collect.py, scores everything, writes the client workbook, and (with --publish)
pushes the slate to Supabase.

  python scripts/process.py --date 2026-10-02 --no-rationale
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline.availability import team_absences  # noqa: E402
from propline.db import load_env, log_run  # noqa: E402
from propline.goalies import goalie_quality  # noqa: E402
from propline.output import build_picks_workbook  # noqa: E402
from propline.publish import publish_slate  # noqa: E402
from propline.scoring import (player_rates, score_player_props,  # noqa: E402
                              score_total_goals)
from propline.storage import upload_workbook  # noqa: E402
from propline.teams import shots_allowed_by_position, team_table  # noqa: E402

ROUND = {"score": 1, "sog_pg_recent": 2, "shots60": 2, "goals60": 2, "assists60": 2,
         "points60": 2, "points_pg_recent": 2, "ppp_pg": 2, "sh_pct": 3,
         "toi_recent": 1, "pp_toi_recent": 1, "toi_bump": 1, "pp_toi_bump": 1,
         "opp_sa_pos": 1, "team_gf": 2, "opp_ga": 2, "opp_pen_taken": 2,
         "opp_pk_pct": 3, "opp_goalie_sv": 3, "home_goalie_sv": 3, "away_goalie_sv": 3,
         "home_gf_pg": 2, "away_gf_pg": 2, "home_ga_pg": 2, "away_ga_pg": 2,
         "combined_pp_threat": 2, "power_score": 1, "points_pct": 3, "pp_pct": 3,
         "pk_pct": 3, "l10_pp_pct": 3, "l10_pk_pct": 3, "sv_pct": 3, "base_toi": 1,
         "base_pp_toi": 1}


def _round(df: pd.DataFrame) -> pd.DataFrame:
    """Round for display. Columns are coerced to numeric first: .round() is a silent
    no-op on object dtype (MLB lesson)."""
    if df is None or df.empty:
        return df
    df = df.copy()
    for c in df.columns:
        if c.endswith("_pg") or c.endswith("_pg_b"):
            df[c] = pd.to_numeric(df[c], errors="coerce").round(2)
        elif c in ROUND:
            df[c] = pd.to_numeric(df[c], errors="coerce").round(ROUND[c])
    return df


# What the model sees per board. Only facts that are shown on that board, so a
# sentence can always be checked against the row beside it.
RATIONALE_FIELDS = {
    "sog": ["sog_pg_recent", "shots60", "opp_sa_pos", "toi_recent"],
    "goals": ["goals_recent", "goals60", "sh_pct", "sog_pg_recent", "opp_goalie",
              "opp_goalie_sv", "opp_goalie_status"],
    "assists": ["assists_recent", "assists60", "pp_toi_recent", "team_gf", "opp_ga"],
    "points": ["points_pg_recent", "points60", "pp_toi_recent", "opp_ga", "opp_goalie",
               "opp_goalie_sv", "opp_goalie_status"],
    "ppp": ["ppp_recent", "ppp_pg", "pp_toi_recent", "opp_pen_taken", "opp_pk_pct"],
}
RATIONALE_COMMON = ["prop", "player_name", "team", "opponent", "recent_games",
                    "toi_bump", "pp_toi_bump", "bump_from"]
TOTALS_FIELDS = ["matchup", "home_gf_pg", "away_gf_pg", "home_ga_pg", "away_ga_pg",
                 "home_goalie", "away_goalie", "home_goalie_sv", "away_goalie_sv",
                 "goalies_status", "missing_names"]


def explain(player_scores, total_goals, top_n):
    """Attach the one-line Why text. Fractions the model would misread (0.141 shooting)
    are sent as percents, and zero bumps are dropped so it does not mention them."""
    from propline.rationale import TOTALS_SYSTEM, add_rationales

    parts = []
    for prop, grp in player_scores.groupby("prop"):
        g = grp.copy()
        send = g.copy()
        for c in ("sh_pct", "opp_pk_pct"):
            send[c] = (100 * send[c]).round(1)
        for c in ("toi_bump", "pp_toi_bump"):
            send[c] = send[c].where(send[c] > 0)
        send["bump_from"] = send["bump_from"].where(
            send["toi_bump"].notna() | send["pp_toi_bump"].notna())
        done = add_rationales(send, RATIONALE_COMMON + RATIONALE_FIELDS[prop], prop,
                              top_n=top_n)
        g["rationale"] = done["rationale"]
        parts.append(g)
    player_scores = pd.concat(parts, ignore_index=True)
    if not total_goals.empty:
        total_goals = add_rationales(total_goals, TOTALS_FIELDS, "total_goals",
                                     top_n=top_n, system=TOTALS_SYSTEM)
    return player_scores, total_goals


def main() -> int:
    ap = argparse.ArgumentParser(description="PropLine NHL — processing")
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--top", type=int, default=40, help="rows per prop tab in Excel")
    ap.add_argument("--publish-top", type=int, default=0,
                    help="rows per prop written to the database; 0 means all")
    ap.add_argument("--no-rationale", action="store_true", help="skip the Groq step")
    ap.add_argument("--explain-top", type=int, default=10,
                    help="picks per board that get a written reason. Kept small: the Groq "
                         "key and its daily token cap are SHARED with PropLine MLB")
    ap.add_argument("--publish", action="store_true", help="write results to Supabase")
    ap.add_argument("--run-kind", default="manual")
    args = ap.parse_args()
    load_env()

    started = datetime.now()
    day = args.date
    logs_dir = Path("data/raw") / day / "logs"
    inter = Path("data/intermediate") / f"collection_{day}.xlsx"
    if not inter.exists():
        print(f"missing {inter} — run scripts/collect.py --date {day} first")
        return 1

    print(f"\n{'='*66}\nPropLine NHL processing — {day}\n{'='*66}")
    x = pd.ExcelFile(inter)
    meta = pd.read_excel(x, "meta").iloc[0]
    season, prev = int(meta["season"]), int(meta["prev_season"])
    schedule = pd.read_excel(x, "schedule")
    teams = pd.read_excel(x, "teams")
    starters = pd.read_excel(x, "goalie_starts")
    avail = pd.read_excel(x, "availability")
    avail["bump_from"] = avail["bump_from"].fillna("")
    team_logs = pd.read_csv(logs_dir / "team_logs.csv")
    goalie_logs = pd.read_csv(logs_dir / "goalie_logs.csv")
    skater_logs = pd.read_csv(logs_dir / "skater_logs.csv")

    # Team stats are league-wide, so the tab is useful even on an off-day.
    print("\n[1/5] Team stats and power rankings")
    team_tbl = team_table(team_logs, teams, season, prev, day)
    no_prior = team_tbl["current_weight"].eq(1.0) & team_tbl["gp"].fillna(0).lt(10)
    print(f"  ok    {len(team_tbl)} teams; top 5: "
          f"{', '.join(team_tbl.head(5)['team'])}")
    if no_prior.any():
        # Usually a renamed or relocated club whose abbreviation changed over the
        # summer, so last season's rows do not join. Visible, not silent.
        print(f"  WARN  no last-season prior for: {', '.join(team_tbl.loc[no_prior, 'team'])}")

    if schedule.empty:
        print("\n  ok    no games scheduled — team stats only")
        player_scores, total_goals = pd.DataFrame(), pd.DataFrame()
        cov = {}
    else:
        print("\n[2/5] Player rates and goalies")
        rates = player_rates(skater_logs, season, prev, day)
        quality = goalie_quality(goalie_logs, season, prev, day)
        sv = dict(zip(quality["goalie_id"], quality["sv_pct"]))
        starters["sv_pct"] = starters["goalie_id"].map(sv)
        sa_pos = shots_allowed_by_position(skater_logs, season, prev, day)
        absences = team_absences(avail)
        print(f"  ok    {len(rates)} skaters with history, {len(quality)} goalies, "
              f"league SV% {quality.attrs['league_sv_pct']:.3f}")

        print("\n[3/5] Scoring")
        player_scores, cov = score_player_props(schedule, avail, rates, team_tbl, sa_pos,
                                                starters, quality, absences)
        total_goals = score_total_goals(schedule, team_tbl, starters, quality, absences)
        print(f"  ok    skaters: {cov}")
        print(f"  ok    total goals: {len(total_goals)}/{len(schedule)} games")
        if len(total_goals) < len(schedule):
            print("  WARN  some games could not be scored — a team is missing from the "
                  "team table")

    player_scores, total_goals = _round(player_scores), _round(total_goals)
    if args.no_rationale:
        print("\n[4/5] Written reasons — SKIPPED (--no-rationale)")
    elif player_scores.empty and total_goals.empty:
        print("\n[4/5] Written reasons — nothing to explain")
    else:
        print(f"\n[4/5] Written reasons (Groq, top {args.explain_top} per board)")
        player_scores, total_goals = explain(player_scores, total_goals, args.explain_top)

    print("\n[5/5] Picks workbook")
    outs = avail[(avail["status"] == "out") & avail["regular"]] if not avail.empty else avail
    team_tbl, starters, outs = _round(team_tbl), _round(starters), _round(outs)
    confirmed_g = int((starters.get("status") == "confirmed").sum()) if not starters.empty else 0
    run_meta = {
        "Slate date": day,
        "Generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "Games": len(schedule),
        "Goalies confirmed": f"{confirmed_g}/{len(starters)} (rest are PROJECTED)",
        "Game rosters posted": f"{int(meta.get('games_with_roster', 0))}/{len(schedule)}",
        "Skaters scored": cov.get("scored", 0),
        "Skipped: out": cov.get("out", 0),
        "Skipped: under 3 games of history": cov.get("too_few_games", 0),
        "Note": ("Scores rank tonight's slate 0-100; they are not probabilities. "
                 "Early season: rates lean on last season until this season has games."),
    }
    out = build_picks_workbook(player_scores, total_goals, team_tbl, starters, outs,
                               Path("data/picks") / f"props_{day}.xlsx", run_meta,
                               top_n=args.top)
    print(f"  ok    {out}")

    if args.publish:
        print("\n  Publishing to Supabase")
        try:
            written = publish_slate(day, schedule, starters, avail, player_scores,
                                    total_goals, team_tbl,
                                    top_n=args.publish_top or None)
            for table, n in written.items():
                print(f"  ok    {table:16} {n} rows")
            try:
                upload_workbook(out, day)
                print("  ok    workbook uploaded")
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN  workbook upload failed: {exc}")
            log_run(day, args.run_kind, "processing", "ok",
                    detail=", ".join(f"{k}={v}" for k, v in written.items()),
                    started_at=started)
        except Exception as exc:
            log_run(day, args.run_kind, "processing", "failed", detail=str(exc)[:400],
                    started_at=started)
            raise
    else:
        print("\n  Publishing — SKIPPED (pass --publish)")

    print(f"\n{'='*66}\nprocessing OK")
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
        if "--publish" in sys.argv:
            _log_crash("processing")
        raise SystemExit(1)
