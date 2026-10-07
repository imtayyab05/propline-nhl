"""Formula-based prop scoring — the decision engine. Not an LLM.

Same method as PropLine MLB: every signal becomes a percentile ACROSS TODAY'S SLATE,
the percentiles are combined with the weights below, and the result is a 0-100 score.
A score is a RANKING within tonight's slate, not a prediction and not a probability.

The hockey part is how the signals are built. A skater's output is roughly
    rate (per 60 minutes)  x  minutes he will play  x  how generous the opponent is
so each prop has an internal "volume" signal (exp_*) built exactly that way, plus a few
supporting signals that catch what the product misses (raw recent form, PP role, the
goalie). The exp_* numbers are INTERNAL and never shown: we have not tested them as
projections, and a number like "3.4 expected shots" beside a 2.5 line would read as a
promise. Only the facts behind them (shots per game, ice time, opponent numbers) are
shown.

Small samples
-------------
October is the hard case: two games of data. Every rate is this season pulled toward
last season, and recent form is pulled toward the season rate, so nobody tops a board
on one hot night.

WEIGHTS is v1 and is meant to be argued with — it is the one part of this system that
encodes betting opinion rather than fact.
"""

from __future__ import annotations

import pandas as pd

WEIGHTS = {
    "sog": {
        "exp_sog": 0.45,              # shot rate x expected minutes x opponent
        "sog_pg_recent": 0.20,        # last 10 games, raw
        "shots60": 0.10,              # season shot rate, ice-time neutral
        "opp_sa_pos": 0.15,           # shots the opponent allows to his position
        "exp_toi": 0.10,
    },
    "goals": {
        "exp_goals": 0.25,            # expected shots x finishing x goalie
        "ixg60": 0.20,                # shot QUALITY: our expected goals per 60
        "goals60": 0.10,
        "sog_pg_recent": 0.10,        # you cannot score without shooting
        "opp_goalie_weak": 0.15,      # projected goalie's save % (inverted)
        "exp_pp_toi": 0.10,           # PP is where goals come cheapest
        "opp_xga": 0.10,              # chance quality the opponent gives up
    },
    "assists": {
        "exp_assists": 0.40,
        "assists60": 0.15,
        "exp_pp_toi": 0.15,
        "team_gf": 0.15,              # you need teammates who finish
        "opp_ga": 0.15,
    },
    "points": {
        "exp_points": 0.40,
        "points_pg_recent": 0.15,
        "exp_pp_toi": 0.15,
        "ixg60": 0.10,                # players who get to dangerous ice create points
        "opp_xga": 0.05,
        "opp_goalie_weak": 0.15,
    },
    "ppp": {
        "exp_ppp": 0.40,              # PP scoring rate x PP minutes x chances
        "exp_pp_toi": 0.20,           # PP1 vs PP2 is most of this prop
        "ppp_pg": 0.10,
        "opp_pen_taken": 0.20,        # how often the opponent goes shorthanded
        "opp_pk_weak": 0.10,          # and how badly it kills them
    },
    "total_goals": {
        "combined_gf": 0.20,          # both teams' scoring
        "combined_ga": 0.20,          # both teams' leakiness
        "combined_xg": 0.20,          # both teams' chance quality, for and against
        "combined_goalie_weak": 0.25,  # both projected goalies
        "combined_pp_threat": 0.10,   # PP% x the other side's penalties
        "combined_shots": 0.05,       # pace
    },
}
PLAYER_PROPS = ("sog", "points", "goals", "assists", "ppp")

RECENT = 10
MIN_GAMES = 3            # combined games below which a skater is not ranked at all
MIN_SHRINK = 0.30        # one recent game still keeps 30% of its own signal
LEAN_GOALIE_WEIGHT = 0.70  # a "lean" projection: 70% him, 30% the backup
SH_PRIOR_SHOTS = 60      # finishing % shrinks toward the league rate by 60 shots
PREV_FLOOR = 0.15        # last season never counts for less than this


def _pct(s: pd.Series) -> pd.Series:
    return s.rank(pct=True).fillna(0.5)


def _weighted(df: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    score = pd.Series(0.0, index=df.index)
    used = 0.0
    for col, w in weights.items():
        if col not in df.columns or df[col].isna().all():
            continue
        score += _pct(df[col]) * w
        used += abs(w)
    return (100 * score / used).round(1) if used else pd.Series(0.0, index=df.index)


def _shrink(recent, baseline, games):
    w = (games.fillna(0) / RECENT).clip(MIN_SHRINK, 1.0)
    base = baseline.fillna(recent)
    return recent.fillna(base) * w + base * (1 - w)


# --- player rates ----------------------------------------------------------------

def player_rates(skater_logs: pd.DataFrame, season: int, prev_season: int,
                 day: str) -> pd.DataFrame:
    """Per skater: season rates (blended with last season) and last-10 form."""
    logs = skater_logs[skater_logs["game_date"] < day].sort_values("game_date")
    if logs.empty:
        return pd.DataFrame()
    cur = logs[logs["season"] == season]
    cur_gp = cur.groupby("player_id").size()

    # last season's weight fades as this season's games accumulate
    lam = (1 - cur_gp / 41).clip(lower=PREV_FLOOR)
    w = logs["player_id"].map(lam).fillna(1.0).where(logs["season"] == prev_season, 1.0)
    w = w.where(logs["season"].isin([season, prev_season]), 0.0)
    stats = ["goals", "assists", "points", "pp_points", "shots", "toi", "pp_toi"]
    weighted = logs[stats].mul(w, axis=0)
    weighted["player_id"] = logs["player_id"]
    weighted["g"] = w
    tot = weighted.groupby("player_id").sum()

    out = pd.DataFrame(index=tot.index)
    toi = tot["toi"].where(tot["toi"] > 0)
    for s in ("shots", "goals", "assists", "points"):
        out[f"{s}60"] = 60 * tot[s] / toi
    out["ppp_per_pp60"] = 60 * tot["pp_points"] / tot["pp_toi"].where(tot["pp_toi"] > 5)
    out["toi_season"] = tot["toi"] / tot["g"].where(tot["g"] > 0)
    out["pp_toi_season"] = tot["pp_toi"] / tot["g"].where(tot["g"] > 0)
    out["ppp_pg"] = tot["pp_points"] / tot["g"].where(tot["g"] > 0)

    # finishing: own shooting % shrunk toward the league rate
    league_sh = logs["goals"].sum() / max(logs["shots"].sum(), 1)
    out["sh_pct"] = (tot["goals"] + league_sh * SH_PRIOR_SHOTS) / (tot["shots"] + SH_PRIOR_SHOTS)

    last = logs.groupby("player_id").tail(RECENT)
    r = last.groupby("player_id").agg(
        recent_games=("toi", "size"), toi_recent=("toi", "mean"),
        pp_toi_recent=("pp_toi", "mean"), sog_pg_recent=("shots", "mean"),
        points_pg_recent=("points", "mean"), goals_recent=("goals", "sum"),
        assists_recent=("assists", "sum"), ppp_recent=("pp_points", "sum"),
        last_game=("game_date", "max"))
    out = out.join(r, how="left")
    out["games_total"] = logs.groupby("player_id").size()
    out["gp_season"] = cur_gp.reindex(out.index).fillna(0).astype(int)
    out.index.name = "player_id"
    return out.reset_index()


# --- player props ------------------------------------------------------------------

def _goalie_sv(starters: pd.DataFrame, quality: pd.DataFrame) -> dict[str, dict]:
    """Expected save % of the goalie each team will face, per team abbrev in net."""
    sv = dict(zip(quality["goalie_id"], quality["sv_pct"]))
    league = quality.attrs.get("league_sv_pct", 0.900)
    out = {}
    for _, s in starters.iterrows():
        main = sv.get(s["goalie_id"], league)
        if s["confidence"] == "lean" and pd.notna(s["backup_id"]):
            back = sv.get(s["backup_id"], league)
            val = LEAN_GOALIE_WEIGHT * main + (1 - LEAN_GOALIE_WEIGHT) * back
        else:
            val = main
        out[s["team"]] = {"sv": val, "name": s["goalie_name"], "status": s["status"],
                          "confidence": s["confidence"]}
    return out


def goalie_check(*sides) -> str:
    """Warning text for picks that lean on a goalie we only have as a 'lean' projection.

    sides = (team, goalie info from _goalie_sv) pairs; returns "" when every goalie
    involved is a high-confidence projection or confirmed. Promised to the client on
    8 Oct 2026 after Buffalo started Ellis over a 'lean' Luukkonen: the warning tells
    him to check his book before betting, rather than pretending we know.
    """
    lean = [f"{(g or {}).get('name') or '?'} ({team})" for team, g in sides
            if (g or {}).get("confidence") == "lean"]
    return ("Not sure to start: " + ", ".join(lean)) if lean else ""


def score_player_props(schedule, avail, rates, team_tbl, sa_pos, starters,
                       quality, absences) -> tuple[pd.DataFrame, dict]:
    """Every eligible skater on tonight's slate, scored for all five props.

    Returns (scores in long form with a `prop` column, coverage counts).
    """
    cov = {"skaters_on_rosters": 0, "out": 0, "too_few_games": 0, "scored": 0}
    if schedule.empty or avail.empty:
        return pd.DataFrame(), cov

    opp_of, game_of = {}, {}
    for _, g in schedule.iterrows():
        opp_of[g["home_team"]], opp_of[g["away_team"]] = g["away_team"], g["home_team"]
        game_of[g["home_team"]] = game_of[g["away_team"]] = g["game_id"]

    cov["skaters_on_rosters"] = len(avail)
    df = avail.copy()
    cov["out"] = int((df["status"] == "out").sum())
    df = df[df["status"] == "in"]
    df = df.merge(rates, on="player_id", how="left")
    thin = df["games_total"].fillna(0) < MIN_GAMES
    cov["too_few_games"] = int(thin.sum())
    df = df[~thin].copy()
    if df.empty:
        return pd.DataFrame(), cov

    df["opponent"] = df["team"].map(opp_of)
    df["game_id"] = df["team"].map(game_of)
    df["lineup_status"] = df["status_source"].map(
        lambda s: "confirmed" if s == "game_roster" else "projected")

    # minutes: recent usage pulled toward season usage, plus any absence bump
    df["exp_toi"] = _shrink(df["toi_recent"], df["toi_season"], df["recent_games"]) + df["toi_bump"]
    df["exp_pp_toi"] = (_shrink(df["pp_toi_recent"], df["pp_toi_season"], df["recent_games"])
                        + df["pp_toi_bump"])

    # opponent context
    t = team_tbl.set_index("team")
    lg = {c: team_tbl[c].mean() for c in ("ga_pg_b", "gf_pg_b", "pen_taken_pg_b",
                                          "pk_pct_b")}
    df["opp_ga"] = df["opponent"].map(t["ga_pg_b"])
    if "xga_pg_b" in t:
        df["opp_xga"] = df["opponent"].map(t["xga_pg_b"])
    df["team_gf"] = df["team"].map(t["gf_pg_b"])
    df["opp_pen_taken"] = df["opponent"].map(t["pen_taken_pg_b"])
    df["opp_pk_pct"] = df["opponent"].map(t["pk_pct_b"])
    df["opp_pk_weak"] = 1 - df["opp_pk_pct"]
    sap = sa_pos.set_index(["team", "pos_group"])["sa_pos_pg_b"]
    df["opp_sa_pos"] = [sap.get((o, p)) for o, p in zip(df["opponent"], df["pos_group"])]
    lg_sap = sa_pos.groupby("pos_group")["sa_pos_pg_b"].mean()

    gk = _goalie_sv(starters, quality)
    lg_sv = quality.attrs.get("league_sv_pct", 0.900)
    df["opp_goalie"] = df["opponent"].map(lambda o: (gk.get(o) or {}).get("name"))
    df["opp_goalie_status"] = df["opponent"].map(
        lambda o: "{status}/{confidence}".format(**gk[o]) if o in gk else "unknown")
    df["opp_goalie_sv"] = df["opponent"].map(lambda o: (gk.get(o) or {}).get("sv", lg_sv))
    df["opp_goalie_weak"] = 1 - df["opp_goalie_sv"]
    df["goalie_check"] = df["opponent"].map(lambda o: goalie_check((o, gk.get(o))))

    ab = absences.set_index("team") if not absences.empty else pd.DataFrame()
    df["opp_missing"] = df["opponent"].map(
        ab["missing_names"] if "missing_names" in ab else {}).fillna("")

    # factors relative to league average, so a generous opponent scales volume up
    f_sa = (df["opp_sa_pos"] / df["pos_group"].map(lg_sap)).fillna(1.0)
    f_ga = (df["opp_ga"] / lg["ga_pg_b"]).fillna(1.0)
    f_gk = (df["opp_goalie_weak"] / (1 - lg_sv)).fillna(1.0)
    f_pen = (df["opp_pen_taken"] / lg["pen_taken_pg_b"]).fillna(1.0)
    f_pk = (df["opp_pk_weak"] / (1 - lg["pk_pct_b"])).fillna(1.0)

    shots60 = _shrink(60 * df["sog_pg_recent"] / df["toi_recent"].where(df["toi_recent"] > 0),
                      df["shots60"], df["recent_games"])
    df["exp_sog"] = shots60 * df["exp_toi"] / 60 * f_sa
    df["exp_goals"] = df["exp_sog"] * df["sh_pct"] * f_gk
    df["exp_assists"] = df["assists60"] * df["exp_toi"] / 60 * f_ga
    df["exp_points"] = df["points60"] * df["exp_toi"] / 60 * (0.5 * f_ga + 0.5 * f_gk)
    df["exp_ppp"] = df["ppp_per_pp60"].fillna(0) * df["exp_pp_toi"] / 60 * f_pen * f_pk ** 0.5

    frames = []
    for prop in PLAYER_PROPS:
        g = df.copy()
        g["prop"] = prop
        g["score"] = _weighted(g, WEIGHTS[prop])
        g["rank"] = g["score"].rank(ascending=False, method="first").astype(int)
        frames.append(g.sort_values("rank"))
    cov["scored"] = len(df)
    return pd.concat(frames, ignore_index=True), cov


# --- total goals ---------------------------------------------------------------------

def score_total_goals(schedule, team_tbl, starters, quality, absences) -> pd.DataFrame:
    if schedule.empty:
        return pd.DataFrame()
    t = team_tbl.set_index("team")
    gk = _goalie_sv(starters, quality)
    lg_sv = quality.attrs.get("league_sv_pct", 0.900)
    ab = absences.set_index("team") if not absences.empty else pd.DataFrame()

    def _ab(team, col):
        return ab[col].get(team, 0) if col in ab else 0

    rows = []
    for _, g in schedule.iterrows():
        h, a = g["home_team"], g["away_team"]
        if h not in t.index or a not in t.index:
            continue
        hg, ag = gk.get(h, {}), gk.get(a, {})
        rows.append({
            "game_id": g["game_id"], "matchup": f"{a} @ {h}",
            "home_team": h, "away_team": a,
            "combined_gf": t.at[h, "gf_pg_b"] + t.at[a, "gf_pg_b"],
            "combined_ga": t.at[h, "ga_pg_b"] + t.at[a, "ga_pg_b"],
            "combined_goalie_weak": (1 - hg.get("sv", lg_sv)) + (1 - ag.get("sv", lg_sv)),
            "combined_pp_threat": (t.at[h, "pp_pct_b"] * t.at[a, "pen_taken_pg_b"] +
                                   t.at[a, "pp_pct_b"] * t.at[h, "pen_taken_pg_b"]),
            "combined_shots": (t.at[h, "sf_pg_b"] + t.at[a, "sf_pg_b"] +
                               t.at[h, "sa_pg_b"] + t.at[a, "sa_pg_b"]) / 2,
            "combined_xg": ((t.at[h, "xgf_pg_b"] + t.at[a, "xgf_pg_b"] +
                             t.at[h, "xga_pg_b"] + t.at[a, "xga_pg_b"]) / 2
                            if "xgf_pg_b" in t else None),
            "home_xgf_pg": t.at[h, "xgf_pg_b"] if "xgf_pg_b" in t else None,
            "away_xgf_pg": t.at[a, "xgf_pg_b"] if "xgf_pg_b" in t else None,
            "home_gf_pg": t.at[h, "gf_pg_b"], "away_gf_pg": t.at[a, "gf_pg_b"],
            "home_ga_pg": t.at[h, "ga_pg_b"], "away_ga_pg": t.at[a, "ga_pg_b"],
            "home_goalie": hg.get("name"), "away_goalie": ag.get("name"),
            "home_goalie_sv": hg.get("sv"), "away_goalie_sv": ag.get("sv"),
            "goalies_status": f"{ag.get('status', '?')} / {hg.get('status', '?')}",
            "goalies_lean": int(hg.get("confidence") == "lean") +
                            int(ag.get("confidence") == "lean"),
            "goalie_check": goalie_check((a, ag), (h, hg)),
            "missing_regulars": int(_ab(h, "missing_regulars") + _ab(a, "missing_regulars")),
            "missing_names": ", ".join(x for x in (_ab(a, "missing_names"),
                                                   _ab(h, "missing_names")) if x),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["score"] = _weighted(df, WEIGHTS["total_goals"])
    df["rank"] = df["score"].rank(ascending=False, method="first").astype(int)
    return df.sort_values("rank").reset_index(drop=True)
