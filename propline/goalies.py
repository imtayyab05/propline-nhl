"""Starting goalies: projected from usage, confirmed only by the league itself.

The official feed has no pre-game starter. What we told the client: the system projects
the likely starter from recent usage — last starts, back-to-backs, rest — and labels it
PROJECTED until confirmed. This file is that projection, built only from official data:

  1. Candidates are the goalies on the club's CURRENT roster. Game logs alone would
     still list a goalie who left in the summer.
  2. The No.1 is whoever has started most of the team's recent games. In the first
     weeks of a season that is a handful of games, so last season's workload acts as a
     prior until the new season has enough starts to speak for itself.
  3. Back-to-back: if the team played yesterday and the No.1 started it, the backup is
     projected. That is the league-wide norm, not a certainty — hence "lean".
  4. If the league has posted tonight's game roster and the projected goalie is not on
     it, switch to the goalie who is.
  5. CONFIRMED only from the boxscore's own `starter` flag, i.e. once the game is on.
     We do not claim confirmation from any source we have not wired in.
"""

from __future__ import annotations

from datetime import timedelta

import pandas as pd

from .nhl import parse_date

RECENT_GAMES = 10          # team games that define "recent usage"
EARLY_SEASON_GAMES = 5     # below this many team games, last season still counts
PRIOR_WEIGHT = 5.0         # last season's start share is worth up to 5 recent starts
HIGH_SHARE = 0.70          # No.1 share at/above which a projection is "high" confidence

# Save-percentage shrinkage. Goalie save % is famously noisy: a hot week means little.
# Every goalie starts from the league average worth this many shots, so a backup with
# 3 good starts cannot outrank an established No.1.
SV_PRIOR_SHOTS = 600
PREV_SEASON_WEIGHT = 0.5   # last season's shots count half

STARTER_COLS = ["game_id", "team", "team_id", "goalie_id", "goalie_name", "status",
                "confidence", "reason", "starts_recent", "team_games_recent",
                "last_start", "back_to_back", "backup_id", "backup_name"]


def project_starters(schedule: pd.DataFrame, rosters: pd.DataFrame,
                     goalie_logs: pd.DataFrame, team_logs: pd.DataFrame,
                     season: int, prev_season: int, day: str,
                     dressed: pd.DataFrame | None = None,
                     confirmed: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per team playing today: who we expect in net and why."""
    if schedule.empty:
        return pd.DataFrame(columns=STARTER_COLS)

    today = parse_date(day)
    yesterday = str(today - timedelta(days=1))
    cur_logs = goalie_logs[goalie_logs["season"] == season]
    prev_logs = goalie_logs[goalie_logs["season"] == prev_season]
    cur_team = team_logs[(team_logs["season"] == season) &
                         (team_logs["game_date"] < day)]
    names = dict(zip(rosters["player_id"], rosters["player_name"]))

    # last season's start share per goalie, whatever team he played for then
    prev_starts = prev_logs.groupby("player_id")["started"].sum()
    prev_share = (prev_starts / 82).clip(upper=1.0)

    rows = []
    for _, g in schedule.iterrows():
        for side in ("home", "away"):
            team, tid = g[f"{side}_team"], g[f"{side}_team_id"]
            cands = rosters[(rosters["team"] == team) & (rosters["pos_group"] == "G")]
            cand_ids = list(cands["player_id"])

            games = cur_team[cur_team["team"] == team].sort_values("game_date")
            recent_ids = list(games["game_id"].tail(RECENT_GAMES))
            n_recent = len(recent_ids)
            starts = (cur_logs[(cur_logs["team"] == team) &
                               cur_logs["game_id"].isin(recent_ids) &
                               (cur_logs["started"] == 1)])

            # If the roster call missed a goalie who has actually been starting, trust
            # the logs: someone in net every night is clearly with the club.
            for pid in starts["player_id"].unique():
                if pid not in cand_ids:
                    cand_ids.append(pid)
                    names.setdefault(pid, starts.loc[starts.player_id == pid,
                                                     "player_name"].iloc[0])

            table = []
            for pid in cand_ids:
                mine = starts[starts["player_id"] == pid]
                n = len(mine)
                last = mine["game_date"].max() if n else None
                key = float(n)
                if n_recent < EARLY_SEASON_GAMES:
                    key += PRIOR_WEIGHT * float(prev_share.get(pid, 0.0))
                table.append({"pid": pid, "starts": n, "last": last or "", "key": key})
            table.sort(key=lambda r: (r["key"], r["last"]), reverse=True)

            row = {"game_id": g["game_id"], "team": team, "team_id": tid,
                   "team_games_recent": n_recent, "back_to_back": False,
                   "status": "projected"}
            if not table:
                rows.append({**row, "goalie_id": None, "goalie_name": None,
                             "confidence": "none", "reason": "no goalie found on roster",
                             "starts_recent": 0, "last_start": None,
                             "backup_id": None, "backup_name": None})
                continue

            no1 = table[0]
            no2 = table[1] if len(table) > 1 else None
            pick, other = no1, no2
            share = no1["starts"] / n_recent if n_recent else None
            if n_recent >= EARLY_SEASON_GAMES:
                reason = f"started {no1['starts']} of last {n_recent}"
                conf = "high" if share is not None and share >= HIGH_SHARE else "lean"
            else:
                ps = float(prev_share.get(no1["pid"], 0.0))
                reason = (f"started {no1['starts']} of {n_recent} so far; "
                          f"{int(round(ps * 82))} starts last season")
                conf = "high" if ps >= 0.6 and (n_recent == 0 or share >= 0.5) else "lean"

            played_yesterday = bool((games["game_date"] == yesterday).any())
            row["back_to_back"] = played_yesterday
            if played_yesterday and no2 is not None and no1["last"] == yesterday:
                pick, other = no2, no1
                reason = "back-to-back: the No.1 started last night"
                conf = "lean"

            # Tonight's official game roster, when the league has posted it.
            if dressed is not None and not dressed.empty:
                dg = dressed[(dressed["game_id"] == g["game_id"]) &
                             (dressed["team_id"] == tid) & (dressed["position"] == "G")]
                if not dg.empty and pick["pid"] not in set(dg["player_id"]):
                    alt = [r for r in table if r["pid"] in set(dg["player_id"])]
                    if alt:
                        other, pick = pick, alt[0]
                        reason = "projected goalie is not on tonight's game roster"
                        conf = "lean"

            # The league's own starter flag — only exists once the game is under way.
            if confirmed is not None and not confirmed.empty:
                cg = confirmed[(confirmed["game_id"] == g["game_id"]) &
                               (confirmed["team_id"] == tid)]
                if not cg.empty:
                    gid = int(cg["goalie_id"].iloc[0])
                    match = [r for r in table if r["pid"] == gid]
                    pick = match[0] if match else {"pid": gid, "starts": 0, "last": ""}
                    other = next((r for r in table if r["pid"] != gid), None)
                    row["status"] = "confirmed"
                    reason, conf = "starter per official boxscore", "confirmed"

            rows.append({**row, "goalie_id": pick["pid"],
                         "goalie_name": names.get(pick["pid"]),
                         "confidence": conf, "reason": reason,
                         "starts_recent": pick["starts"],
                         "last_start": pick["last"] or None,
                         "backup_id": other["pid"] if other else None,
                         "backup_name": names.get(other["pid"]) if other else None})

    return pd.DataFrame(rows, columns=STARTER_COLS)


def goalie_quality(goalie_logs: pd.DataFrame, season: int, prev_season: int,
                   day: str) -> pd.DataFrame:
    """Shrunk save % per goalie: this season + half of last season + a league prior."""
    gl = goalie_logs[goalie_logs["game_date"] < day].copy()
    if gl.empty:
        return pd.DataFrame(columns=["goalie_id", "sv_pct", "sv_pct_raw", "shots_faced"])
    w = gl["season"].map({season: 1.0, prev_season: PREV_SEASON_WEIGHT}).fillna(0.0)
    gl["w_saves"] = gl["saves"] * w
    gl["w_shots"] = gl["shots_against"] * w
    league = gl["saves"].sum() / max(gl["shots_against"].sum(), 1)
    agg = gl.groupby("player_id").agg(w_saves=("w_saves", "sum"),
                                      w_shots=("w_shots", "sum"))
    cur = gl[gl["season"] == season].groupby("player_id").agg(
        s=("saves", "sum"), a=("shots_against", "sum"))
    out = pd.DataFrame({
        "goalie_id": agg.index,
        "sv_pct": ((agg["w_saves"] + league * SV_PRIOR_SHOTS) /
                   (agg["w_shots"] + SV_PRIOR_SHOTS)).values,
    })
    out["sv_pct_raw"] = out["goalie_id"].map(cur["s"] / cur["a"].where(cur["a"] > 0))
    out["shots_faced"] = out["goalie_id"].map(cur["a"]).fillna(0).astype(int)
    out.attrs["league_sv_pct"] = league
    return out
