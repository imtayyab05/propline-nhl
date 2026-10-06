"""Build the expected-goals model and last season's shot-quality totals. Run once per
season (and whenever the model changes); the daily pipeline only reads the outputs.

  python scripts/build_xg.py                     # season inferred from today
  python scripts/build_xg.py --fit-season 20252026

Pulls every regular-season play-by-play of the fit season once (~1,312 requests to the
free NHL feed, cached in data/cache/{season}/shots.csv), then writes three small files
that ARE committed, so CI never has to repeat the pull:

  propline/data/xg_table.csv          the fitted model (goal rate per shot cell)
  propline/data/xg_prev_players.csv   per-player season totals (the early-season prior)
  propline/data/xg_prev_teams.csv     per-team season totals

It also checks the model honestly: total xG against actual goals on the fit season AND
on this season's games so far, which the model has never seen.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from propline import nhl  # noqa: E402
from propline.shots import (DATA, apply_xg, danger_threshold, fit_xg,  # noqa: E402
                            player_game_xg, season_shots, team_game_xg)


def calibration(label: str, s: pd.DataFrame) -> None:
    s = s[s["xg"].notna()]
    print(f"\n  {label}: {len(s):,} attempts, {int(s.goal.sum()):,} goals, "
          f"{s.xg.sum():,.0f} xG  (ratio {s.goal.sum() / s.xg.sum():.3f})")
    for k, g in s.groupby("strength"):
        print(f"        {k}: goals {int(g.goal.sum()):5}  xG {g.xg.sum():7.1f}")
    s = s.assign(dec=pd.qcut(s["xg"].rank(method="first"), 10, labels=False))
    d = s.groupby("dec").agg(xg=("xg", "mean"), actual=("goal", "mean"), n=("xg", "size"))
    print("        deciles (mean xG -> actual goal rate):")
    for i, r in d.iterrows():
        print(f"          {i}: {r.xg:.3f} -> {r.actual:.3f}  (n={int(r.n):,})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-season", type=int, default=None)
    args = ap.parse_args()
    cur = nhl.season_of(date.today())
    fit = args.fit_season or nhl.previous_season(cur)

    teams = nhl.get_teams()
    abbr = dict(zip(teams["team_id"], teams["abbrev"]))

    def games(season):
        p = Path("data/cache") / str(season) / "team_logs.csv"
        logs = pd.read_csv(p) if p.exists() else nhl.team_game_logs(season, teams)
        logs = logs[logs["game_type"] == nhl.REGULAR]
        return sorted(logs["game_id"].unique())

    print(f"[1/4] Play-by-play for {fit} (one-off, cached)")
    fit_ids = games(fit)
    shots = season_shots(fit_ids, fit, Path("data/cache") / str(fit) / "shots.csv",
                         progress=True)
    print(f"  ok    {len(shots):,} unblocked attempts from {shots.game_id.nunique()} "
          f"games ({shots.attrs['fetched']} fetched now, {shots.attrs['failed']} failed)")

    print("\n[2/4] Fit")
    table = fit_xg(shots)
    DATA.mkdir(exist_ok=True)
    table.to_csv(DATA / "xg_table.csv", index=False)
    hd = danger_threshold(table)
    print(f"  ok    {len(table)} cells; dangerous shot = xG >= {hd:.3f}")

    print("\n[3/4] Checks")
    shots["xg"] = apply_xg(shots, table)
    calibration(f"fit season {fit}", shots)
    cur_logs = nhl.team_game_logs(cur, teams)
    cur_ids = sorted(cur_logs.loc[cur_logs["game_type"] == nhl.REGULAR, "game_id"].unique())
    if cur_ids:
        held = season_shots(cur_ids, cur, Path("data/cache") / str(cur) / "shots.csv")
        held["xg"] = apply_xg(held, table)
        calibration(f"this season {cur} (never seen by the fit)", held)

    print("\n[4/4] Last season's totals (the early-season prior)")
    pg = player_game_xg(shots, hd)
    players = pg.groupby("player_id").agg(
        games=("game_id", "nunique"), attempts=("attempts", "sum"), ixg=("ixg", "sum"),
        hd_attempts=("hd_attempts", "sum"), pp_ixg=("pp_ixg", "sum")).reset_index()
    players.insert(0, "season", fit)
    players.round(4).to_csv(DATA / "xg_prev_players.csv", index=False)
    tg = team_game_xg(shots, hd)
    tm = tg.groupby("team_id").agg(games=("game_id", "nunique"), xgf=("xgf", "sum"),
                                   xga=("xga", "sum"), hdf=("hdf", "sum"),
                                   hda=("hda", "sum")).reset_index()
    tm.insert(1, "team", tm["team_id"].map(abbr))
    tm.insert(0, "season", fit)
    tm.round(3).to_csv(DATA / "xg_prev_teams.csv", index=False)
    (DATA / "xg_meta.txt").write_text(f"fit_season={fit}\ndanger_threshold={hd:.5f}\n")
    print(f"  ok    {len(players)} players, {len(tm)} teams -> {DATA}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
