"""Projected goals and the model's lean against the market (client ask, 19 Oct 2026).

READABILITY ONLY: no score or rank reads anything here. The boards still rank on the
percentile weights in scoring.py / games.py; this turns the same team numbers into a
goals figure, so the Total Goals board can say which side of the market line the model
is on and the Team Goals board can tag clear 2.5 / 3.5 team-total spots.

It is a DIRECTION, never a probability. A lean says the projection sits on one side of
the line by a margin; it does not say how often that side wins.

Projection for one team tonight (goals per game):

    attack  = half its GF/G + half its xGF/G
    defence = half the opponent's GA/G + half the opponent's xGA/G x goalie factor
    goals   = K x attack x defence / league GF/G  +/- half the measured home edge

All rates are the blended ones (this season pulled toward last season). xG is put on the
goals scale first: raw xG misses empty-netters and finishing skill, so comparing raw
combined xG with a market total would lean UNDER on every game. The goalie factor is
(1 - tonight's shrunk SV%) / (1 - league SV%), applied to the xGA half only: GA already
carries the opponent's usual goalie. K is set every run so that the league-average
projection equals this season's actual league-average goals per team-game.
"""

from __future__ import annotations

import pandas as pd

# Thresholds set from 85 games with a market total, 8-19 Oct 2026. The projection
# averaged 6.16 against the market's 6.12 and sat above it on 45, below on 38, so the
# calibration has no lean of its own.
#  - 0.3 badged 38 of 85 games (nearly half: not "meaningful"); 0.4 badges 24 (13 over,
#    11 under).
#  - team tags: the average team scores ~3.07, so "0.3 over 2.5" tagged 109 of 170
#    team-games. 0.75 tags roughly the top quarter.
LEAN_MARGIN = 0.4            # goals between the projected and market total before a lean shows
TAG_MARGIN = 0.75            # goals above 2.5 / 3.5 before a team-total tag shows
TAG_LINES = (3.5, 2.5)       # his book's team-total milestones, highest first
MIN_CUR_GAMES = 40           # team-games this season before its scoring level is trusted


def _calibration(team_tbl: pd.DataFrame, team_logs: pd.DataFrame, season: int,
                 prev: int, day: str) -> dict:
    t = team_tbl.set_index("team")
    logs = team_logs[(team_logs["game_type"] == 2) & (team_logs["game_date"] < day)]
    cur = logs[logs["season"] == season]
    last = logs[logs["season"] == prev]
    # this season's actual scoring is the target; with almost no games yet, last season's
    target = cur["goals_for"].mean() if len(cur) >= MIN_CUR_GAMES else last["goals_for"].mean()
    lg_gf = t["gf_pg_b"].mean()
    xg_scale = (lg_gf / t["xgf_pg_b"].mean()) if "xgf_pg_b" in t and t["xgf_pg_b"].mean() else None

    # measured home edge (goals per team-game, home minus road), last season
    h = last[last["home_road"] == "H"]["goals_for"].mean()
    r = last[last["home_road"] == "R"]["goals_for"].mean()
    home_edge = (h - r) if len(last) > 400 else 0.0

    base = {"lg_gf": lg_gf, "xg_scale": xg_scale, "home_edge": home_edge, "k": 1.0}
    # K: average over every ordered pairing at a neutral site with a league-average goalie
    teams = list(t.index)
    vals = [_raw(t, a, b, 1.0, base) for a in teams for b in teams if a != b]
    vals = [v for v in vals if v == v]
    base["k"] = target / (sum(vals) / len(vals)) if vals else 1.0
    base.update(target=target, cur_games=len(cur))
    return base


def _raw(t: pd.DataFrame, team: str, opp: str, gfac: float, c: dict) -> float:
    xs = c["xg_scale"]
    if xs:
        attack = 0.5 * t.at[team, "gf_pg_b"] + 0.5 * xs * t.at[team, "xgf_pg_b"]
        defence = 0.5 * t.at[opp, "ga_pg_b"] + 0.5 * xs * t.at[opp, "xga_pg_b"] * gfac
    else:
        attack, defence = t.at[team, "gf_pg_b"], t.at[opp, "ga_pg_b"] * gfac
    return c["k"] * attack * defence / c["lg_gf"]


def project(total_goals: pd.DataFrame, boards: dict, team_tbl: pd.DataFrame,
            team_logs: pd.DataFrame, season: int, prev: int, day: str,
            lg_sv: float) -> tuple[pd.DataFrame, dict, dict]:
    """Add proj_goals to Team Goals and proj_total to Total Goals. Returns the
    calibration so the run log can show it."""
    c = _calibration(team_tbl, team_logs, season, prev, day)
    t = team_tbl.set_index("team")
    tg = boards.get("team_goals")
    if tg is None or tg.empty:
        return total_goals, boards, c
    tg = tg.copy()
    proj = []
    for _, r in tg.iterrows():
        sv = r.get("opp_goalie_sv")
        gfac = (1 - sv) / (1 - lg_sv) if sv == sv and sv is not None else 1.0
        v = _raw(t, r["team"], r["opponent"], gfac, c)
        v += c["home_edge"] / 2 * (1 if r["side"] == "home" else -1)
        proj.append(round(v, 2))
    tg["proj_goals"] = proj
    boards = {**boards, "team_goals": tg}
    if total_goals is not None and not total_goals.empty:
        per_game = tg.groupby("game_id")["proj_goals"].sum().round(2)
        total_goals = total_goals.copy()
        total_goals["proj_total"] = total_goals["game_id"].map(per_game)
    return total_goals, boards, c


def add_leans(total_goals: pd.DataFrame, boards: dict) -> tuple[pd.DataFrame, dict]:
    """After market lines are attached. A lean or tag shows only when the gap is at
    least the margin AND every goalie it depends on is a firm projection or confirmed
    (starters are only ever confirmed once the game is on, so requiring confirmed would
    mean no lean before puck drop). A "lean" goalie, or no market total, means blank."""
    if total_goals is not None and not total_goals.empty and "proj_total" in total_goals:
        g = total_goals.copy()
        out = []
        for _, r in g.iterrows():
            m, p = r.get("market_total"), r.get("proj_total")
            if m is None or m != m or p is None or p != p or r.get("goalies_lean", 0) > 0                     or not r.get("home_goalie") or not r.get("away_goalie"):
                out.append(None)
            elif p - m >= LEAN_MARGIN:
                out.append("MODEL LEAN: OVER")
            elif m - p >= LEAN_MARGIN:
                out.append("MODEL LEAN: UNDER")
            else:
                out.append(None)
        g["model_lean"] = out
        g["proj_vs_market"] = (g["proj_total"] - pd.to_numeric(g.get("market_total"),
                                                               errors="coerce")).round(2)
        total_goals = g
    tg = boards.get("team_goals")
    if tg is not None and not tg.empty and "proj_goals" in tg:
        tg = tg.copy()
        tags = []
        for _, r in tg.iterrows():
            p = r["proj_goals"]
            tag = None
            if r.get("opp_goalie_conf") in ("high", "confirmed"):
                for line in TAG_LINES:
                    if p - line >= TAG_MARGIN:
                        tag = f"Team Total Over {line}"
                        break
            tags.append(tag)
        tg["tt_tag"] = tags
        boards = {**boards, "team_goals": tg}
    return total_goals, boards
