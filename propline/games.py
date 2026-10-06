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

from .scoring import _goalie_sv, _weighted

WEIGHTS = {
    # team goals tonight
    "team_goals": {
        "gf": 0.30,                # its own scoring rate
        "opp_ga": 0.25,            # what the opponent gives up
        "opp_goalie_weak": 0.25,   # the goalie it faces (1 - shrunk save %)
        "pp_threat": 0.10,         # its PP% x the opponent's penalties
        "l10_gf": 0.10,            # current form
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
    "goal_diff_pg_b": 0.35,       # the best single measure of team strength
    "shot_diff_pg_b": 0.20,       # steadier than goals
    "l10_goal_diff_pg": 0.10,     # form
    "special_teams_b": 0.10,      # PP% + PK%
}
GOALIE_WEIGHT = 0.20     # tonight's goalie, per .010 of save % above/below league
HOME_ICE_FALLBACK = 0.13 # used only if last season's logs are missing; see home_ice_edge
BACK_TO_BACK = -0.15     # second night of a back-to-back
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
    return float(STRENGTH["goal_diff_pg_b"] * gd / sg + STRENGTH["shot_diff_pg_b"] * sd / ss)


def team_context(schedule, team_tbl, starters, quality, absences,
                 home_ice: float = HOME_ICE_FALLBACK) -> pd.DataFrame:
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
    zs = pd.DataFrame({c: _z(tz[c]) for c in STRENGTH})
    base = sum(zs[c] * w for c, w in STRENGTH.items())

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
                        + (BACK_TO_BACK if b2b.get(team) else 0.0)
                        + MISSING_PER_60 * missing / 60)
            rows.append({
                "game_id": g["game_id"], "team": team, "opponent": opp, "side": side,
                "matchup": f"{g['away_team']} @ {g['home_team']}",
                "home_team": g["home_team"], "away_team": g["away_team"],
                "gf": t.at[team, "gf_pg_b"], "ga": t.at[team, "ga_pg_b"],
                "gd": t.at[team, "gf_pg_b"] - t.at[team, "ga_pg_b"],
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
                "opp_goalie": theirs.get("name"), "opp_goalie_sv": theirs.get("sv"),
                "opp_goalie_weak": 1 - theirs.get("sv", lg_sv),
                "opp_goalie_status": theirs.get("status"),
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
    zs = pd.DataFrame({c: _z(team_tbl[c]) for c in STRENGTH})
    s = sum(zs[c] * w for c, w in STRENGTH.items()).to_numpy()
    gaps = pd.Series([abs(x - y) for i, x in enumerate(s) for y in s[i + 1:]])
    return float(gaps.quantile(1 / 3)), float(gaps.quantile(2 / 3))


def score_game_boards(schedule, team_tbl, starters, quality, absences,
                      team_logs: pd.DataFrame | None = None,
                      prev_season: int | None = None) -> dict[str, pd.DataFrame]:
    """Every Phase 2 board for tonight, keyed by prop."""
    home_ice = (home_ice_edge(team_logs, team_tbl, prev_season)
                if team_logs is not None and prev_season else HOME_ICE_FALLBACK)
    ctx = team_context(schedule, team_tbl, starters, quality, absences, home_ice)
    if ctx.empty:
        return {}
    boards = {p: _rank(ctx, WEIGHTS[p]) for p in TEAM_PROPS}

    g = _pair(ctx)
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
