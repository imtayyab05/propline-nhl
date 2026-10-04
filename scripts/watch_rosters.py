"""One-off probe: when does the league post each game's roster before puck drop?

The run schedule depends on this (CLAUDE.md, data decision 2). Polls the official feed
for one day's games and records the first moment each game's roster (play-by-play
rosterSpots) appears, and how many minutes before the scheduled start that was.
Stops by itself once every game has started. Costs nothing: no keys, no quotas.

  python scripts/watch_rosters.py                  # today, poll every 5 minutes
  python scripts/watch_rosters.py --date 2026-10-03 --every 5

Results: data/roster_timing_{date}.csv (also printed as it goes).
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline import nhl  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--every", type=int, default=5, help="minutes between checks")
    args = ap.parse_args()

    sched = nhl.get_schedule(args.date)
    if sched.empty:
        print("no games")
        return 0
    start = {r.game_id: datetime.fromisoformat(r.game_time_utc.replace("Z", "+00:00"))
             for r in sched.itertuples()}
    label = {r.game_id: f"{r.away_team}@{r.home_team}" for r in sched.itertuples()}
    out = Path("data") / f"roster_timing_{args.date}.csv"
    out.parent.mkdir(exist_ok=True)
    seen: dict[int, datetime] = {}
    print(f"watching {len(start)} games, writing {out}")

    while True:
        now = datetime.now(timezone.utc)
        for gid in start:
            if gid in seen or now > start[gid]:
                continue
            try:
                pbp = nhl._get(f"{nhl.WEB}/gamecenter/{gid}/play-by-play")
            except nhl.FeedError as exc:
                print(f"{now:%H:%M} {label[gid]} error {exc}")
                continue
            spots = pbp.get("rosterSpots") or []
            if spots:
                seen[gid] = now
                mins = (start[gid] - now).total_seconds() / 60
                goalies = sum(1 for s in spots if s.get("positionCode") == "G")
                row = [args.date, gid, label[gid], start[gid].isoformat(),
                       now.isoformat(timespec="seconds"), round(mins), len(spots), goalies]
                new = not out.exists()
                with open(out, "a", newline="") as fh:
                    w = csv.writer(fh)
                    if new:
                        w.writerow(["date", "game_id", "game", "start_utc", "seen_utc",
                                    "minutes_before", "players", "goalies"])
                    w.writerow(row)
                print(f"{now:%H:%M} UTC  {label[gid]:9} roster posted {round(mins)} min "
                      f"before start ({len(spots)} players, {goalies} goalies)")
        if all(now > s for s in start.values()):
            missed = [label[g] for g in start if g not in seen]
            if missed:
                print(f"started without a posted roster seen: {', '.join(missed)}")
            print("done")
            return 0
        time.sleep(args.every * 60)


if __name__ == "__main__":
    raise SystemExit(main())
