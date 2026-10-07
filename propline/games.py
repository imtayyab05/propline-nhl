"""Phase 2 game and team boards: SOG, power-play goals, team goals, moneyline, puck line.

Same method as every other board (see propline/scoring.py): signals become percentiles
ACROSS TONIGHT'S SLATE and are combined with the weights below into a 0-100 score. A
score ranks tonight's games against each other. It is not a projection and never a
probability.

Moneyline and puck line follow what we told the client: a matchup-strength RATING shown
beside the market line, never a win %. The 10,000-simulation boards he subscribes to
print 88-99% win probabilities; over 1-3 Oct 2026 their top-rated plays went 9/15 and
7/14. A number that looks like a probability and is not one is the thing to avoid.

Every signal is a team number this pipeline already has (propline/teams.py: this
season pulled toward last season while the sample is small) plus tonight's context:
the projected goalie, back-to-backs, and regulars who are out.
"""

from __future__ import annotations

import pandas as pd

from .scoring import _goalie_sv, _weighted, goalie_check

WEIGHTS = {
    # team goals tonight
    "team_goals": {
        "gf": 0.20,                # its own scoring rate
        "xgf": 0.15,               # the quality of chances it creates
        "opp_ga": 0.15,            # what the opponent gives up
        "opp_xga": 0.10,           # the quality of chances the opponent allows
        "opp_goalie_weak": 0.25,   # the goalie it faces (1 - shrunk save %)
        "pp_threat": 0.10,         # its PP% x the opponent's penalties
        "l10_gf": 0.05,            # current form
    },
    "team_sog": {
        "sf": 0.40,                # shots it generates
        "opp_sa": 0.35,            # shots the opponent concedes
        "l10_sf": 0.15,
        "opp_l10_sa": 0.10,
    },
    "game_sog": {
        "pace": 0.70,              # both teams' shots for and against, season
        "l10_pace": 0.30,          # the same over the last 10
    },
    "team_ppg": {
        "pp_threat": 0.45,         # PP% x how often the opponent goes shorthanded
        "ppg": 0.20,               # PP goals it scores per game
        "opp_ppga": 0.20,          # PP goals the opponent allows per game
        "opp_pk_weak": 0.15,       # 1 - opponent PK%
    },
    "game_ppg": {
        "pp_threat_sum": 0.60,
        "ppg_sum": 0.20,
        "ppga_sum": 0.20,
    },
    # puck line: the stronger side covering -1.5
    "puck_line": {
        "gap": 0.60,               # how much stronger it is (see STRENGTH)
        "margin_fuel": 0.25,       # its scoring + the other side's leakiness
        "dog_goalie_weak": 0.15,   # the goalie it faces
    },
}

# Team strength for moneyline / puck line, in league-wide z-scores (so the weights are
# comparable). v1 opinion, meant to be argued with like WEIGHTS.
STRENGTH = {
    "goal_diff_pg_b": 0.25,       # the best single measure of team strength
    "xg_diff_pg_b": 0.15,         # chance quality for minus against (propline/shots.py)
    "shot_diff_pg_b": 0.15,       # steadier than goals
    "l10_goal_diff_pg": 0.10,     # form
    "special_teams_b": 0.10,      # PP% + PK%
}
GOALIE_WEIGHT = 0.20     # tonight's goalie, per .010 of save % above/below league
HOME_ICE_FALLBACK = 0.13 # used only if last season's logs are missing; see home_ice_edge
BACK_TO_BACK_FALLBACK = {"home": -0.24, "away": -0.29}   # used only without last season's logs
MISSING_PER_60 = -0.10   # per 60 minutes of regular ice time out tonight

TEAM_PROPS = ("team_goals", "team_sog", "team_ppg")
GAME_PROPS = ("game_sog", "game_ppg", "moneyline", "puck_line")


def _z(s: pd.Series) -> pd.Series:
    sd = s.std()
    return (s - s.mean()) / sd if sd and sd == sd else s * 0


def _rank(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    df = df.copy()
    df["score"] = _weighted(df, weights)
    df["rank"] = df["score"].rank(ascending=False, method="first").astype(int)
    return df.sort_values("rank").reset_index(drop=True)


def home_ice_edge(team_logs: pd.DataFrame, team_tbl: pd.DataFrame, prev_season: int) -> float:
    """Home advantage in strength units, MEASURED from last season rather than guessed.

    v1 guessed 0.05; on 6 Oct 2026 the market favoured the home side in every one of the
    4 games where it disagreed with us. Measured: home teams outscored visitors by 0.136
    goals and 1.09 shots a game over 1,312 games (52.2% home wins) = 0.127 here.
    """
    prev = team_logs[(team_logs["season"] == prev_season) & (team_logs["game_type"] == 2)
                     & (team_logs["home_road"] == "H")]
    sg, ss = team_tbl["goal_diff_pg_b"].std(), team_tbl["shot_diff_pg_b"].std()
    if len(prev) < 200 or not sg or not ss:
        return HOME_ICE_FALLBACK
    gd = (prev["goals_for"] - prev["goals_against"]).mean()
    sd = (prev["shots_for"] - prev["shots_against"]).mean()
    wg, ws = STRENGTH["goal_diff_pg_b"], STRENGTH["shot_diff_pg_b"]
    # The xG term has a home edge too, but last season's shot file does not record which
    # side was home. Assume it matches the goal + shot edge, scaled to its weight, rather
    # than letting the xG weight silently shrink home ice.
    wx = STRENGTH.get("xg_diff_pg_b", 0.0) if "xg_diff_pg_b" in team_tbl else 0.0
    return float((wg * gd / sg + ws * sd / ss) * (wg + ws + wx) / (wg + ws))


def back_to_back_edges(team_logs: pd.DataFrame, team_tbl: pd.DataFrame,
                       prev_season: int) -> dict[str, float]:
    """Back-to-back penalty in strength units, MEASURED from last season, home and road
    separately. v1 used a flat -0.15 guess; the client's Gemini review (8 Oct 2026)
    pointed at Ottawa on a road back-to-back, and the measurement says the guess was
    about half the real effect.

    Measured on the SKATERS only. On a back-to-back the goalie projection already
    switches to the backup and the goalie term already prices him, so goals against are
    replaced by what the team's normal goaltending would have allowed on those shots -
    otherwise the backup-goalie effect would be counted twice. Each second night is
    compared with the same team's other games at the same venue, so home ice (added
    separately) is not counted twice either.

    Measured 8 Oct 2026 on 2025-26: road -0.31 goals/game skater-side (n=269) = -0.29
    units; home -0.21 (n=161, noisier) = -0.24 units.
    """
    tl = team_logs[(team_logs["season"] == prev_season) & (team_logs["game_type"] == 2)].copy()
    sg, ss = team_tbl["goal_diff_pg_b"].std(), team_tbl["shot_diff_pg_b"].std()
    if len(tl) < 1000 or not sg or not ss:
        return dict(BACK_TO_BACK_FALLBACK)
    tl["date"] = pd.to_datetime(tl["game_date"])
    tl = tl.sort_values(["team", "date"])
    tl["b2b"] = (tl["date"] - tl.groupby("team")["date"].shift()).dt.days == 1
    tot = tl.groupby("team")[["goals_against", "shots_against"]].sum()
    miss = tot["goals_against"] / tot["shots_against"]
    tl["adj_gd"] = tl["goals_for"] - tl["shots_against"] * tl["team"].map(miss)
    tl["sd"] = tl["shots_for"] - tl["shots_against"]
    normal = tl[~tl["b2b"]].groupby(["team", "home_road"])[["adj_gd", "sd"]].mean()
    wg, ws = STRENGTH["goal_diff_pg_b"], STRENGTH["shot_diff_pg_b"]
    wx = STRENGTH.get("xg_diff_pg_b", 0.0) if "xg_diff_pg_b" in team_tbl else 0.0
    out = {}
    for venue, side in (("H", "home"), ("R", "away")):
        b = tl[tl["b2b"] & (tl["home_road"] == venue)].join(normal, on=["team", "home_road"],
                                                             rsuffix="_n")
        if len(b) < 50:
            out[side] = BACK_TO_BACK_FALLBACK[side]
            continue
        d_gd = (b["adj_gd"] - b["adj_gd_n"]).mean()
        d_sd = (b["sd"] - b["sd_n"]).mean()
        # same scaling as home_ice_edge: the xG term is assumed to move with goals+shots
        out[side] = float((wg * d_gd / sg + ws * d_sd / ss) * (wg + ws + wx) / (wg + ws))
    return out


def team_context(schedule, team_tbl, starters, quality, absences,
                 home_ice: float = HOME_ICE_FALLBACK,
                 b2b_edge: dict | None = None) -> pd.DataFrame:
    """One row per team playing tonight, with its own and its opponent's numbers."""
    if schedule.empty:
        return pd.DataFrame()
    t = team_tbl.set_index("team")
    gk = _goalie_sv(starters, quality)
    lg_sv = quality.attrs.get("league_sv_pct", 0.900)
    b2b = dict(zip(starters["team"], starters["back_to_back"])) if not starters.empty else {}
    ab = absences.set_index("team") if not absences.empty else pd.DataFrame()

    # league-wide z-scores, so a team is measured against the league, not the slate
    tz = team_tbl.set_index("team")
    used = {c: w for c, w in STRENGTH.items() if c in tz and tz[c].notna().any()}
    zs = pd.DataFrame({c: _z(tz[c]) for c in used})
    base = sum(zs[c].fillna(0) * w for c, w in used.items())

    rows = []
    for _, g in schedule.iterrows():
        for side, team, opp in (("home", g["home_team"], g["away_team"]),
                                ("away", g["away_team"], g["home_team"])):
            if team not in t.index or opp not in t.index:
                continue
            mine, theirs = gk.get(team, {}), gk.get(opp, {})
            missing = 0.0
            if "missing_f_toi" in ab:
                missing = float(ab["missing_f_toi"].get(team, 0) + ab["missing_d_toi"].get(team, 0))
            strength = (float(base.get(team, 0.0))
                        + GOALIE_WEIGHT * (mine.get("sv", lg_sv) - lg_sv) / 0.010
                        + (home_ice if side == "home" else 0.0)
                        + ((b2b_edge or BACK_TO_BACK_FALLBACK)[side] if b2b.get(team) else 0.0)
                        + MISSING_PER_60 * missing / 60)
            rows.append({
                "game_id": g["game_id"], "team": team, "opponent": opp, "side": side,
                "matchup": f"{g['away_team']} @ {g['home_team']}",
                "home_team": g["home_team"], "away_team": g["away_team"],
                "gf": t.at[team, "gf_pg_b"], "ga": t.at[team, "ga_pg_b"],
                "gd": t.at[team, "gf_pg_b"] - t.at[team, "ga_pg_b"],
                "xgf": t.at[team, "xgf_pg_b"] if "xgf_pg_b" in t else None,
                "opp_xga": t.at[opp, "xga_pg_b"] if "xga_pg_b" in t else None,
                "opp_ga": t.at[opp, "ga_pg_b"], "opp_gf": t.at[opp, "gf_pg_b"],
                "l10_gf": t.at[team, "l10_gf_pg"] if "l10_gf_pg" in t else None,
                "sf": t.at[team, "sf_pg_b"], "sa": t.at[team, "sa_pg_b"],
                "opp_sa": t.at[opp, "sa_pg_b"], "opp_sf": t.at[opp, "sf_pg_b"],
                "l10_sf": t.at[team, "l10_sf_pg"] if "l10_sf_pg" in t else None,
                "opp_l10_sa": t.at[opp, "l10_sa_pg"] if "l10_sa_pg" in t else None,
                "ppg": t.at[team, "ppg_pg_b"], "opp_ppga": t.at[opp, "ppga_pg_b"],
                "pp_pct": t.at[team, "pp_pct_b"], "opp_pk_pct": t.at[opp, "pk_pct_b"],
                "opp_pk_weak": 1 - t.at[opp, "pk_pct_b"],
                "opp_pen_taken": t.at[opp, "pen_taken_pg_b"],
                "pp_threat": t.at[team, "pp_pct_b"] * t.at[opp, "pen_taken_pg_b"],
                "goalie": mine.get("name"), "goalie_sv": mine.get("sv"),
                "goalie_status": mine.get("status"),
                "goalie_conf": mine.get("confidence"),
                "opp_goalie": theirs.get("name"), "opp_goalie_sv": theirs.get("sv"),
                "opp_goalie_weak": 1 - theirs.get("sv", lg_sv),
                "opp_goalie_status": theirs.get("status"),
                "opp_goalie_conf": theirs.get("confidence"),
                # team boards score this team's offence: the goalie it SHOOTS AT matters
                "goalie_check": goalie_check((opp, theirs)),
                "back_to_back": bool(b2b.get(team)), "missing_toi": round(missing, 1),
                "missing_names": ab["missing_names"].get(team, "") if "missing_names" in ab else "",
                "strength": strength,
            })
    return pd.DataFrame(rows)


def _pair(ctx: pd.DataFrame) -> pd.DataFrame:
    """One row per game with home_* and away_* columns side by side."""
    h = ctx[ctx["side"] == "home"].set_index("game_id").add_prefix("home_")
    a = ctx[ctx["side"] == "away"].set_index("game_id").add_prefix("away_")
    g = h.join(a, how="inner")
    g["matchup"] = g["home_matchup"]
    return g.reset_index()


def strength_bands(team_tbl: pd.DataFrame) -> tuple[float, float]:
    """Thresholds for Slight / Moderate / Strong edges: the terciles of the strength gap
    across EVERY possible pairing in the league, so the bands are measured, not made up,
    and a one-game slate still gets an honest label."""
    used = {c: w for c, w in STRENGTH.items()
            if c in team_tbl and team_tbl[c].notna().any()}
    zs = pd.DataFrame({c: _z(team_tbl[c]) for c in used})
    s = sum(zs[c].fillna(0) * w for c, w in used.items()).to_numpy()
    gaps = pd.Series([abs(x - y) for i, x in enumerate(s) for y in s[i + 1:]])
    return float(gaps.quantile(1 / 3)), float(gaps.quantile(2 / 3))


def score_game_boards(schedule, team_tbl, starters, quality, absences,
                      team_logs: pd.DataFrame | None = None,
                      prev_season: int | None = None) -> dict[str, pd.DataFrame]:
    """Every Phase 2 board for tonight, keyed by prop."""
    home_ice = (home_ice_edge(team_logs, team_tbl, prev_season)
                if team_logs is not None and prev_season else HOME_ICE_FALLBACK)
    b2b_edge = (back_to_back_edges(team_logs, team_tbl, prev_season)
                if team_logs is not None and prev_season else dict(BACK_TO_BACK_FALLBACK))
    ctx = team_context(schedule, team_tbl, starters, quality, absences, home_ice, b2b_edge)
    if ctx.empty:
        return {}
    boards = {p: _rank(ctx, WEIGHTS[p]) for p in TEAM_PROPS}

    g = _pair(ctx)
    # game boards depend on both goalies
    g["goalie_check"] = [
        goalie_check((a, {"name": an, "confidence": ac}), (h, {"name": hn, "confidence": hc}))
        for a, an, ac, h, hn, hc in zip(g["away_team"], g["away_goalie"], g["away_goalie_conf"],
                                         g["home_team"], g["home_goalie"], g["home_goalie_conf"])]
    g["pace"] = g["home_sf"] + g["home_sa"] + g["away_sf"] + g["away_sa"]
    g["l10_pace"] = (g["home_l10_sf"] + g["away_l10_sf"] +
                     g["home_opp_l10_sa"] + g["away_opp_l10_sa"])
    boards["game_sog"] = _rank(g, WEIGHTS["game_sog"])

    g["pp_threat_sum"] = g["home_pp_threat"] + g["away_pp_threat"]
    g["ppg_sum"] = g["home_ppg"] + g["away_ppg"]
    g["ppga_sum"] = g["home_opp_ppga"] + g["away_opp_ppga"]
    boards["game_ppg"] = _rank(g, WEIGHTS["game_ppg"])

    # moneyline: who is stronger tonight, and by how much
    lo, hi = strength_bands(team_tbl)
    g["gap_signed"] = g["home_strength"] - g["away_strength"]
    g["gap"] = g["gap_signed"].abs()
    g["pick"] = g["home_team"].where(g["gap_signed"] >= 0, g["away_team"])
    g["dog"] = g["away_team"].where(g["gap_signed"] >= 0, g["home_team"])
    g["edge"] = g["gap"].map(lambda x: "Strong" if x >= hi else "Moderate" if x >= lo else "Slight")
    fav_home = g["gap_signed"] >= 0
    g["pick_goalie"] = g["home_goalie"].where(fav_home, g["away_goalie"])
    g["dog_goalie"] = g["away_goalie"].where(fav_home, g["home_goalie"])
    # Real numbers for each side, so the written reason can cite facts rather than the
    # internal strength index (which it quoted as "margin fuel of 6.67" in testing).
    for col in ("gd", "gf", "ga", "goalie_sv"):
        g[f"pick_{col}"] = g[f"home_{col}"].where(fav_home, g[f"away_{col}"])
        g[f"dog_{col}"] = g[f"away_{col}"].where(fav_home, g[f"home_{col}"])
    g["pick_b2b"] = g["home_back_to_back"].where(fav_home, g["away_back_to_back"])
    g["dog_b2b"] = g["away_back_to_back"].where(fav_home, g["home_back_to_back"])
    ml = g.copy()
    ml["score"] = (100 * ml["gap"].rank(pct=True)).round(1)
    ml["rank"] = ml["score"].rank(ascending=False, method="first").astype(int)
    boards["moneyline"] = ml.sort_values("rank").reset_index(drop=True)

    # puck line: the stronger side winning by two or more
    g["margin_fuel"] = (g["home_gf"].where(fav_home, g["away_gf"]) +
                        g["away_ga"].where(fav_home, g["home_ga"]))
    g["dog_goalie_weak"] = g["home_opp_goalie_weak"].where(fav_home, g["away_opp_goalie_weak"])
    pl = _rank(g, WEIGHTS["puck_line"])
    pl["subject"] = pl["pick"] + " -1.5"
    boards["puck_line"] = pl
    return boards
