"""Who is out tonight, and who absorbs their minutes (the client's add-on).

Sources, official feed only:
  - Recent games: a player on the current roster who did not dress for his team's most
    recent game(s) is treated as OUT until he plays again. This catches injuries AND
    scratches without needing an injury report, and it is how a long absence is known.
  - Tonight's game roster (play-by-play rosterSpots), when the league has posted it,
    overrides everything: on it means in, off it means out. This is the only
    same-day confirmation the feed gives.

Known blind spot, flagged rather than hidden: a player hurt in practice today is not
known until the game roster posts. Until then he is scored as playing.

Minutes redistribution
----------------------
When a regular is out, his ice time does not vanish — the coach hands it to teammates.
  - Even-strength / all-situations time goes to the same position group (forwards to
    forwards, D to D), in proportion to how much each already plays. A call-up takes
    the bottom-of-lineup share (REPLACEMENT_TOI), so only the rest is redistributed.
  - Power-play time goes entirely to the players already on the power play, in
    proportion to their PP time — the second unit moves up. This is the "especially if
    he was offensive" part: a PP quarterback's absence moves PPP chances most.
  - An absence that is already several games old is already inside teammates' recent
    averages. Only the part not yet reflected is added, so a long-term injury is not
    counted twice.
"""

from __future__ import annotations

import pandas as pd

RECENT = 10                                # games behind "base" ice time
REGULAR_TOI = {"F": 14.0, "D": 17.0}       # minutes/game that make a player a regular
REPLACEMENT_TOI = {"F": 10.0, "D": 15.0}   # what a call-up typically plays
PP_REGULAR = 1.0                           # PP minutes/game to count as on the PP
MAX_BUMP = 4.0                             # cap on extra minutes any one player gets
DRESSED_PER_TEAM = 20                      # 18 skaters + 2 goalies: a trimmed, final list

AVAIL_COLS = ["team", "player_id", "player_name", "pos_group", "position", "base_toi",
              "base_pp_toi", "regular", "games_missed", "status", "status_source",
              "reason", "toi_bump", "pp_toi_bump", "bump_from"]


def _base_usage(skater_logs: pd.DataFrame, day: str) -> pd.DataFrame:
    """Average TOI and PP TOI over each player's last RECENT games (any team, any
    season) — his normal workload, independent of where he plays now."""
    logs = skater_logs[skater_logs["game_date"] < day].sort_values("game_date")
    last = logs.groupby("player_id").tail(RECENT)
    return last.groupby("player_id").agg(base_toi=("toi", "mean"),
                                         base_pp_toi=("pp_toi", "mean"),
                                         base_games=("toi", "size"))


def availability(schedule: pd.DataFrame, rosters: pd.DataFrame,
                 skater_logs: pd.DataFrame, team_logs: pd.DataFrame,
                 season: int, day: str,
                 dressed: pd.DataFrame | None = None) -> pd.DataFrame:
    if schedule.empty or rosters.empty:
        return pd.DataFrame(columns=AVAIL_COLS)

    playing = set(schedule["home_team"]) | set(schedule["away_team"])
    team_game = {}
    for _, g in schedule.iterrows():
        team_game[g["home_team"]] = (g["game_id"], g["home_team_id"])
        team_game[g["away_team"]] = (g["game_id"], g["away_team_id"])

    sk = rosters[rosters["team"].isin(playing) & rosters["pos_group"].isin(["F", "D"])]
    base = _base_usage(skater_logs, day)
    df = sk.merge(base, left_on="player_id", right_index=True, how="left")
    df["base_toi"] = df["base_toi"].fillna(0.0)
    df["base_pp_toi"] = df["base_pp_toi"].fillna(0.0)
    df["regular"] = df["base_toi"] >= df["pos_group"].map(REGULAR_TOI)

    cur_games = team_logs[(team_logs["season"] == season) & (team_logs["game_date"] < day)]
    cur_sk = skater_logs[(skater_logs["season"] == season) & (skater_logs["game_date"] < day)]

    missed, status, source, reason = [], [], [], []
    for _, p in df.iterrows():
        team_ids = list(cur_games[cur_games["team"] == p["team"]]
                        .sort_values("game_date")["game_id"])
        played = set(cur_sk[(cur_sk["player_id"] == p["player_id"]) &
                            (cur_sk["team"] == p["team"])]["game_id"])
        streak = 0
        for gid in reversed(team_ids):
            if gid in played:
                break
            streak += 1
        missed.append(streak)

        gid, tid = team_game[p["team"]]
        dg = (dressed[(dressed["game_id"] == gid) & (dressed["team_id"] == tid)]
              if dressed is not None and not dressed.empty else pd.DataFrame())
        on_list = None if dg.empty else p["player_id"] in set(dg["player_id"])
        # The league posts an EXTENDED list (~23 per team, extras included) about two
        # hours before puck drop and trims it to the 20 who dress only later. Measured
        # on 3 Oct 2026: 13 games, lists posted 96-156 min before start, 43-46 names.
        # So before the trim, absence from the list is proof of OUT, but presence is
        # not proof of playing.
        final = on_list is not None and len(dg) <= DRESSED_PER_TEAM
        if on_list is False:
            status.append("out")
            source.append("game_roster")
            reason.append("not on tonight's game-day roster")
        elif final:
            status.append("in")
            source.append("game_roster")
            reason.append("dressed tonight (final game roster)")
        elif not team_ids:
            status.append("in")
            source.append("none")
            reason.append("team has not played yet this season — no absence data")
        elif streak:
            status.append("out")
            source.append("recent_games")
            reason.append(f"missed last {streak} game{'s' if streak > 1 else ''}"
                          if streak < len(team_ids) else
                          f"has not played for {p['team']} this season")
        else:
            status.append("in")
            source.append("recent_games")
            reason.append("played last game")
        if on_list and not final:
            reason[-1] += "; on tonight's extended roster (not yet final)"
    df["games_missed"] = missed
    df["status"] = status
    df["status_source"] = source
    df["reason"] = reason

    return _redistribute(df)


def _redistribute(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["toi_bump"] = 0.0
    df["pp_toi_bump"] = 0.0
    df["bump_from"] = ""
    for team, t in df.groupby("team"):
        outs = t[(t["status"] == "out") & t["regular"]]
        for _, o in outs.iterrows():
            # Share of teammates' recent window in which he was still playing — the
            # part of his absence their averages have not absorbed yet.
            unseen = max(0.0, 1.0 - min(int(o["games_missed"]), RECENT) / RECENT)
            if unseen <= 0:
                continue
            grp = o["pos_group"]
            mates = t[(t["status"] == "in") & (t["pos_group"] == grp) & t["regular"]]
            ev = max(o["base_toi"] - REPLACEMENT_TOI[grp], 0.0) * unseen
            if ev > 0 and mates["base_toi"].sum() > 0:
                share = mates["base_toi"] / mates["base_toi"].sum()
                df.loc[mates.index, "toi_bump"] += ev * share
            pp = o["base_pp_toi"] * unseen
            pp_mates = t[(t["status"] == "in") & (t["base_pp_toi"] >= PP_REGULAR)]
            if pp >= 0.5 and pp_mates["base_pp_toi"].sum() > 0:
                share = pp_mates["base_pp_toi"] / pp_mates["base_pp_toi"].sum()
                df.loc[pp_mates.index, "pp_toi_bump"] += pp * share
            touched = mates.index.union(pp_mates.index if pp >= 0.5 else pd.Index([]))
            for i in touched:
                df.at[i, "bump_from"] = (df.at[i, "bump_from"] + ", " if df.at[i, "bump_from"]
                                         else "") + o["player_name"]
    df["toi_bump"] = df["toi_bump"].clip(upper=MAX_BUMP).round(2)
    df["pp_toi_bump"] = df["pp_toi_bump"].clip(upper=MAX_BUMP).round(2)
    return df[AVAIL_COLS]


def team_absences(avail: pd.DataFrame) -> pd.DataFrame:
    """Per team: how much regular ice time is missing — the 'both ends of the ice'
    effect. A team without its top D pair gives up more; without its top line, scores
    less. Shown on the boards and fed into the game-level totals."""
    cols = ["team", "missing_regulars", "missing_f_toi", "missing_d_toi",
            "missing_pp_toi", "missing_names"]
    if avail.empty:
        return pd.DataFrame(columns=cols)
    out = avail[(avail["status"] == "out") & avail["regular"]]
    rows = []
    for team in avail["team"].unique():
        o = out[out["team"] == team]
        rows.append({"team": team, "missing_regulars": len(o),
                     "missing_f_toi": round(o.loc[o.pos_group == "F", "base_toi"].sum(), 1),
                     "missing_d_toi": round(o.loc[o.pos_group == "D", "base_toi"].sum(), 1),
                     "missing_pp_toi": round(o["base_pp_toi"].sum(), 1),
                     "missing_names": ", ".join(o["player_name"])})
    return pd.DataFrame(rows, columns=cols)
