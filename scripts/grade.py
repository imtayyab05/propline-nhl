"""Grade finished slates against the final box scores (Results / Track Record page).

    python scripts/grade.py                      # every slate frozen but not yet graded
    python scripts/grade.py --date 2026-10-22    # one slate (re-grades it)
    python scripts/grade.py --date 2026-10-21 --test-from-boards
        # TEST ONLY: grade the stored boards of a past slate and print the table. Those
        # boards came from the slate's last run, after most games had started, so this
        # never writes anything.

Runs on the morning slot (daily.yml), after the publish. Reads pick_snapshots (each
game's picks as of its last run before puck drop, see propline/results.py), the
official boxscores for the final scores, and the stats API for player lines. Writes
pick_results and a pipeline_runs row (stage "grading"). Never touches a score.
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline import results  # noqa: E402
from propline.db import check_json, delete_where, load_env, log_run, read, upsert  # noqa: E402

LOOKBACK_DAYS = 7           # how far back an ungraded slate is picked up
GIVE_UP_DAYS = 2            # after this, a game still not final is graded VOID


def _eastern_today() -> date:
    return datetime.now(ZoneInfo("America/New_York")).date()


def read_all(table: str, params: dict) -> list[dict]:
    out, off = [], 0
    while True:
        page = read(table, {**params, "limit": "1000", "offset": str(off)})
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def pending_slates(today: date) -> list[str]:
    """Slates with a snapshot and no results, from the last week, before today."""
    out = []
    for i in range(LOOKBACK_DAYS, 0, -1):
        d = (today - timedelta(days=i)).isoformat()
        has_snap = read("pick_snapshots", {"select": "game_id", "slate_date": f"eq.{d}",
                                           "limit": "1"})
        if not has_snap:
            continue
        has_res = read("pick_results", {"select": "game_id", "slate_date": f"eq.{d}",
                                        "limit": "1"})
        if not has_res:
            out.append(d)
    return out


def grade_one(day: str, today: date, test: bool) -> tuple[pd.DataFrame, str]:
    if test:
        snaps = results.boards_as_snapshots(day)
    else:
        snaps = pd.DataFrame(read_all("pick_snapshots",
                                      {"select": "*", "slate_date": f"eq.{day}"}))
    if snaps.empty:
        return pd.DataFrame(), "no snapshot"
    gids = sorted(snaps["game_id"].astype(int).unique())
    games = results.final_scores(gids)
    unfinished = [g for g, v in games.items() if v.get("state") not in results.FINAL]
    age = (today - date.fromisoformat(day)).days
    if unfinished and age < GIVE_UP_DAYS and not test:
        return pd.DataFrame(), f"waiting: {len(unfinished)} game(s) not final"
    logs = results.day_logs(day)
    res = results.grade(day, snaps, games, logs)
    note = (f"{len(gids)} games ({len(unfinished)} not completed -> void), "
            f"{len(logs)} player lines")
    return res, note


def show(res: pd.DataFrame, title: str) -> None:
    s = results.summarise(res)
    print(f"\n{title}")
    if s.empty:
        print("  (nothing graded)")
        return
    print(f"  {'board':28} {'bucket':11} {'picks':>5} {'hit rate':>9} {'base':>6}")
    for _, r in s.iterrows():
        base = f"{r['base_rate']:.0%}" if r["base_rate"] == r["base_rate"] else "-"
        print(f"  {r['board']:28} {r['bucket']:11} {r['picks']:>5} "
              f"{r['hit_rate']:>8.0%} {base:>6}   ({r['hits']}/{r['picks']})")


def main() -> int:
    ap = argparse.ArgumentParser(description="PropLine NHL - grade finished slates")
    ap.add_argument("--date", help="grade this slate (YYYY-MM-DD) even if graded before")
    ap.add_argument("--test-from-boards", action="store_true",
                    help="grade the stored boards instead of the snapshots; prints only")
    ap.add_argument("--run-kind", default="manual")
    args = ap.parse_args()
    load_env()
    today = _eastern_today()
    started = datetime.now(timezone.utc)

    days = [args.date] if args.date else pending_slates(today)
    print(f"PropLine NHL grading - {today}: "
          f"{', '.join(days) if days else 'nothing pending'}")
    total = []
    for day in days:
        res, note = grade_one(day, today, args.test_from_boards)
        print(f"  {day}: {note}")
        if res.empty:
            continue
        counts = res["result"].value_counts().to_dict()
        detail = (f"{len(res)} picks: {counts.get('hit', 0)} hit / {counts.get('miss', 0)} "
                  f"miss / {counts.get('void', 0)} void | {note}")
        if args.test_from_boards:
            show(res, f"TEST - {day} graded from the stored boards (not a fair record)")
            continue
        check_json(res)
        delete_where("pick_results", {"slate_date": f"eq.{day}"})
        upsert("pick_results", res, on_conflict="slate_date,board,game_id,subject")
        log_run(day, args.run_kind, "grading", "ok", detail=detail, started_at=started)
        print(f"  ok    {detail}")
        total.append(res)

    if not args.test_from_boards:
        # Snapshots are only needed until graded; results keep the record.
        cutoff = (today - timedelta(days=results.PRUNE_DAYS)).isoformat()
        try:
            delete_where("pick_snapshots", {"slate_date": f"lt.{cutoff}"})
        except Exception as exc:  # noqa: BLE001 - housekeeping only
            print(f"  WARN  snapshot prune failed: {exc}")
        if total:
            show(pd.concat(total, ignore_index=True), "Graded this run")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        try:
            load_env()
            day = sys.argv[sys.argv.index("--date") + 1] if "--date" in sys.argv \
                else _eastern_today().isoformat()
            kind = sys.argv[sys.argv.index("--run-kind") + 1] if "--run-kind" in sys.argv \
                else "manual"
            if "--test-from-boards" not in sys.argv:
                log_run(day, kind, "grading", "failed",
                        detail=traceback.format_exc().strip().splitlines()[-1][:400])
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(1)
