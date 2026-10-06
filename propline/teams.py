"""Team for/against tables and power rankings — the tab that replaces MoneyPuck.

Two kinds of number live here and must not be confused:
  - DISPLAY numbers: this season only, and this season's last 10 games. What the client
    reads on the tab. Early in October they rest on two or three games and say so (gp).
  - SIGNAL numbers (suffix _b, "blended"): this season pulled toward last season by
    PRIOR_GP games' worth of last season's rate. Used by scoring and the power score,
    so that a team that went 2-0 in week one is not crowned the best in the league.
    As games pile up, this season takes over: at 10 GP it is half, at 40 GP 80%.
"""

from __future__ import annotations

import pandas as pd

PRIOR_GP = 10
RECENT = 10

POWER_WEIGHTS = {
    "goal_diff_pg_b": 0.25,       # the best single measure of team strength
    "xg_diff_pg_b": 0.15,         # chance quality for minus against (propline/shots.py)
    "shot_diff_pg_b": 0.20,       # steadier than goals, less luck
    "points_pct_b": 0.15,         # results
    "l10_goal_diff_pg": 0.15,     # current form
    "special_teams_b": 0.10,      # PP% + PK%
}

# per-game quantities summed from the logs, and how each is named for display
COUNTS = {"goals_for": "gf", "goals_against": "ga", "shots_for": "sf",
          "shots_against": "sa", "pp_goals_for": "ppg", "pp_goals_against": "ppga",
          "times_shorthanded": "pen_taken"}


def _summarise(g: pd.DataFrame) -> dict:
    gp = len(g)
    out = {"gp": gp}
    if not gp:
        return out
    for col, short in COUNTS.items():
        out[f"{short}_pg"] = g[col].sum() / gp
    out["pp_pct"] = g["pp_goals_for"].sum() / max(g["pp_opps"].sum(), 1)
    out["pk_pct"] = 1 - g["pp_goals_against"].sum() / max(g["times_shorthanded"].sum(), 1)
    w, otl = int(g["win"].sum()), int(g["otl"].sum())
    out["record"] = f"{w}-{gp - w - otl}-{otl}"
    out["points"] = int(g["points"].sum())
    out["points_pct"] = out["points"] / (2 * gp)
    return out


def team_table(team_logs: pd.DataFrame, teams: pd.DataFrame, season: int,
               prev_season: int, day: str, xg: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per team in the league: season, last-10 and blended numbers."""
    logs = team_logs[team_logs["game_date"] < day].sort_values("game_date")
    cur = logs[logs["season"] == season]
    prev = logs[(logs["season"] == prev_season) & (logs["game_type"] == 2)]
    active = sorted(set(cur["team"]) | set(prev["team"]))

    rows = []
    for team in active:
        c = cur[cur["team"] == team]
        p = prev[prev["team"] == team]
        s = _summarise(c)
        l10 = _summarise(c.tail(RECENT))
        ps = _summarise(p)
        row = {"team": team, **s}
        row.update({f"l10_{k}": v for k, v in l10.items() if k != "points"})

        gp = s.get("gp", 0)
        wc = gp / (gp + PRIOR_GP) if ps.get("gp") else 1.0
        for k in [f"{x}_pg" for x in COUNTS.values()] + ["pp_pct", "pk_pct", "points_pct"]:
            cv, pv = s.get(k), ps.get(k)
            if cv is None and pv is None:
                row[f"{k}_b"] = None
            elif cv is None:
                row[f"{k}_b"] = pv
            elif pv is None:
                row[f"{k}_b"] = cv
            else:
                row[f"{k}_b"] = wc * cv + (1 - wc) * pv
        row["current_weight"] = round(wc, 2)
        rows.append(row)

    df = pd.DataFrame(rows)
    ids = dict(zip(teams["abbrev"], teams["team_id"]))
    names = dict(zip(teams["abbrev"], teams["team_name"]))
    df["team_id"] = df["team"].map(ids)
    df["team_name"] = df["team"].map(names)

    df["goal_diff_pg_b"] = df["gf_pg_b"] - df["ga_pg_b"]
    df["shot_diff_pg_b"] = df["sf_pg_b"] - df["sa_pg_b"]
    df["special_teams_b"] = df["pp_pct_b"] + df["pk_pct_b"]
    # Last-10 goal difference, shrunk toward the blended season number when the
    # window is short — two games of form is mostly noise.
    l10gd = df.get("l10_gf_pg", pd.Series(dtype=float)) - df.get("l10_ga_pg",
                                                                   pd.Series(dtype=float))
    w = (df.get("l10_gp", 0).fillna(0) / RECENT).clip(0, 1)
    df["l10_goal_diff_pg"] = (l10gd.fillna(df["goal_diff_pg_b"]) * w +
                              df["goal_diff_pg_b"] * (1 - w))

    if xg is not None and not xg.empty:
        df = df.merge(xg, on="team_id", how="left")

    # A missing input (no shot model) drops out and the rest are re-weighted, rather
    # than every team scoring the same on it.
    score, used = pd.Series(0.0, index=df.index), 0.0
    for col, wt in POWER_WEIGHTS.items():
        if col in df and df[col].notna().any():
            score += df[col].rank(pct=True).fillna(0.5) * wt
            used += wt
    df["power_score"] = (100 * score / used).round(1)
    df["power_rank"] = df["power_score"].rank(ascending=False, method="min").astype(int)
    return df.sort_values("power_rank").reset_index(drop=True)


def shots_allowed_by_position(skater_logs: pd.DataFrame, season: int,
                              prev_season: int, day: str) -> pd.DataFrame:
    """Shots on goal each team gives up to forwards and to defensemen, per game.

    Built from the skater logs' opponent column, so it costs no extra requests. A
    defence that lets point shots through is a different matchup for a shooting D-man
    than one that collapses and blocks, and team-level shots-against cannot see that.
    Blended with last season the same way as team_table.
    """
    logs = skater_logs[skater_logs["game_date"] < day].copy()
    if logs.empty:
        return pd.DataFrame(columns=["team", "pos_group", "sa_pos_pg_b"])
    logs["pos_group"] = logs["position"].map(lambda p: "D" if p == "D" else "F")
    per_game = (logs.groupby(["season", "opponent", "game_id", "pos_group"])["shots"]
                .sum().reset_index())
    agg = (per_game.groupby(["season", "opponent", "pos_group"])["shots"]
           .agg(["sum", "count"]).reset_index())
    agg["pg"] = agg["sum"] / agg["count"]
    cur = agg[agg["season"] == season].set_index(["opponent", "pos_group"])
    prev = agg[agg["season"] == prev_season].set_index(["opponent", "pos_group"])
    keys = cur.index.union(prev.index)
    rows = []
    for k in keys:
        gp = int(cur["count"].get(k, 0))
        cv = cur["pg"].get(k)
        pv = prev["pg"].get(k)
        if pv is None or pd.isna(pv):
            v = cv
        elif cv is None or pd.isna(cv):
            v = pv
        else:
            wc = gp / (gp + PRIOR_GP)
            v = wc * cv + (1 - wc) * pv
        rows.append({"team": k[0], "pos_group": k[1], "sa_pos_pg_b": v})
    return pd.DataFrame(rows)
