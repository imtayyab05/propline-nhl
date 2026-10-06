"""Shot quality: our own expected-goals (xG) model, built from the official play-by-play.

What we told the client: we build from the official NHL feed — the same raw data
MoneyPuck builds from — and our shot-quality measure is our own. This is that measure.

How it works
------------
Every UNBLOCKED shot attempt (goal, shot on goal, missed shot) in the play-by-play has
x/y coordinates, a shot type and a strength state. A blocked shot is recorded where it
was BLOCKED, not where it was taken, so blocked attempts are left out — standard
practice, and the honest choice when the location would be wrong.

The model is a lookup table fitted on last season: the share of attempts that became
goals, by distance x angle x shot type x strength (even / power play / shorthanded).
Thin cells are pulled toward the coarser cell above them, so a rare combination cannot
produce a wild rate. Empty-net attempts are excluded from the fit and from player xG:
they say nothing about shot quality.

Orientation (verified 6 Oct 2026 on game 2026020024): `homeTeamDefendingSide` "left"
means the home goalie is in the left net, so the home team shoots at x = +89; it flips
each period. situationCode digits are [away goalie][away skaters][home skaters]
[home goalie]; a 0 goalie digit means the net is empty.

Cost: one play-by-play request per finished game. Last season's ~1,312 games are pulled
ONCE by scripts/build_xg.py, which commits only the small fitted table and per-player /
per-team season totals (propline/data/). This season's games are pulled incrementally
and cached, so a scheduled run fetches only the games finished since the last run.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd

from .nhl import WEB, FeedError, _get

NET_X = 89
ATTEMPTS = {"goal", "shot-on-goal", "missed-shot"}
DIST_BINS = [0, 10, 15, 20, 25, 30, 40, 50, 60, 250]
ANGLE_BINS = [0, 20, 35, 50, 91]
TYPE_GROUP = {
    "wrist": "wrist", "snap": "wrist", "slap": "slap", "backhand": "backhand",
    "tip-in": "tip", "deflected": "tip", "bat": "tip", "between-legs": "tip",
    "wrap-around": "wrap", "poke": "other", "cradle": "other",
}
SHRINK = 60            # attempts' worth of weight given to the coarser cell

SHOT_COLS = ["game_id", "season", "period", "team_id", "shooter_id", "goalie_id",
             "event", "goal", "x", "y", "distance", "angle", "shot_type",
             "strength", "empty_net"]

DATA = Path(__file__).resolve().parent / "data"


# --- extraction ---------------------------------------------------------------------

def _strength(code: str, shooter_home: bool) -> tuple[str, bool]:
    """('EV'|'PP'|'SH', empty_net) for the SHOOTING team, from a situationCode."""
    if not code or len(code) != 4 or not code.isdigit():
        return "EV", False
    away_g, away_s, home_s, home_g = (int(c) for c in code)
    own, opp = (home_s, away_s) if shooter_home else (away_s, home_s)
    defending_goalie = away_g if shooter_home else home_g
    # a pulled goalie adds a skater; compare skaters excluding that extra attacker
    strength = "PP" if own > opp else "SH" if own < opp else "EV"
    return strength, defending_goalie == 0


def game_shots(game_id: int, season: int) -> list[dict]:
    pbp = _get(f"{WEB}/gamecenter/{game_id}/play-by-play")
    home_id = pbp["homeTeam"]["id"]
    rows = []
    for e in pbp.get("plays", []):
        kind = e.get("typeDescKey")
        if kind not in ATTEMPTS:
            continue
        d = e.get("details", {})
        x, y = d.get("xCoord"), d.get("yCoord")
        team = d.get("eventOwnerTeamId")
        side = e.get("homeTeamDefendingSide")
        if x is None or y is None or team is None or side not in ("left", "right"):
            continue
        shooter_home = team == home_id
        # which net this team attacks this period
        attack_right = (side == "left") == shooter_home
        xs = x if attack_right else -x
        dx = NET_X - xs
        dist = math.hypot(dx, y)
        angle = 90.0 if dx <= 0 else math.degrees(math.atan(abs(y) / dx))
        strength, empty = _strength(e.get("situationCode", ""), shooter_home)
        rows.append({
            "game_id": game_id, "season": season,
            "period": e.get("periodDescriptor", {}).get("number"),
            "team_id": team,
            "shooter_id": d.get("scoringPlayerId") if kind == "goal" else d.get("shootingPlayerId"),
            "goalie_id": d.get("goalieInNetId"), "event": kind, "goal": int(kind == "goal"),
            "x": xs, "y": y, "distance": round(dist, 1), "angle": round(angle, 1),
            "shot_type": TYPE_GROUP.get(d.get("shotType"), "other"),
            "strength": strength, "empty_net": empty,
        })
    return rows


def season_shots(game_ids, season: int, cache_csv: Path, workers: int = 4,
                 progress: bool = False, checkpoint: int = 200) -> pd.DataFrame:
    """All attempts for these games, fetching only games not already in the cache.

    Finished games never change, so the cache only ever grows. A game that fails to
    load is skipped and retried next run rather than sinking the run. The cache is
    saved every `checkpoint` games, so an interrupted season pull resumes, not restarts.
    A few requests run in parallel: the first full-season pull took ~1h one at a time.
    """
    from concurrent.futures import ThreadPoolExecutor

    cache_csv = Path(cache_csv)
    cache_csv.parent.mkdir(parents=True, exist_ok=True)
    have = pd.read_csv(cache_csv) if cache_csv.exists() else pd.DataFrame(columns=SHOT_COLS)
    done = set(have["game_id"].astype("int64")) if not have.empty else set()
    # A game with no attempts at all would be refetched forever; remember those too.
    marker = cache_csv.with_suffix(".empty")
    empty = set(int(x) for x in marker.read_text().split()) if marker.exists() else set()
    todo = [int(g) for g in game_ids if int(g) not in done and int(g) not in empty]

    def one(gid):
        try:
            return gid, game_shots(gid, season)
        except FeedError:
            return gid, None

    frames, failed, batch = [have], 0, []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, (gid, rows) in enumerate(pool.map(one, todo), 1):
            if rows is None:
                failed += 1
            elif rows:
                batch += rows
            else:
                empty.add(gid)
            if n % checkpoint == 0 or n == len(todo):
                frames.append(pd.DataFrame(batch, columns=SHOT_COLS))
                batch = []
                pd.concat(frames, ignore_index=True).to_csv(cache_csv, index=False)
                if progress:
                    print(f"        {n}/{len(todo)} games")
    out = pd.concat(frames, ignore_index=True)
    if not todo:
        out.to_csv(cache_csv, index=False)
    if empty:
        marker.write_text(" ".join(str(g) for g in sorted(empty)))
    out.attrs["fetched"], out.attrs["failed"] = len(todo) - failed, failed
    return out


# --- model --------------------------------------------------------------------------

def _bins(shots: pd.DataFrame) -> pd.DataFrame:
    s = shots.copy()
    s["dbin"] = pd.cut(s["distance"], DIST_BINS, right=False, labels=False).fillna(-1).astype(int)
    s["abin"] = pd.cut(s["angle"], ANGLE_BINS, right=False, labels=False).fillna(-1).astype(int)
    return s


# coarse -> fine; each level's rate is shrunk toward the level above it
LEVELS = [["dbin"], ["dbin", "abin"], ["dbin", "abin", "shot_type"],
          ["dbin", "abin", "shot_type", "strength"]]


def fit_xg(shots: pd.DataFrame) -> pd.DataFrame:
    """Goal rate per finest cell (distance x angle x type x strength)."""
    s = _bins(shots[~shots["empty_net"].astype(bool)])
    base = s["goal"].mean()
    parent = None
    for keys in LEVELS:
        g = s.groupby(keys)["goal"].agg(goals="sum", n="count").reset_index()
        if parent is None:
            g["prior"] = base
        else:
            g = g.merge(parent[keys[:-1] + ["rate"]].rename(columns={"rate": "prior"}),
                        on=keys[:-1], how="left")
            g["prior"] = g["prior"].fillna(base)
        g["rate"] = (g["goals"] + SHRINK * g["prior"]) / (g["n"] + SHRINK)
        parent = g
    return parent[LEVELS[-1] + ["goals", "n", "rate"]]


def apply_xg(shots: pd.DataFrame, table: pd.DataFrame) -> pd.Series:
    """xG per attempt; NaN for empty-net attempts. A cell the fit never saw falls back
    to that distance band's overall rate."""
    s = _bins(shots)
    m = s[LEVELS[-1]].merge(table[LEVELS[-1] + ["rate"]], on=LEVELS[-1], how="left")
    if m["rate"].isna().any():
        tot = table.groupby("dbin")[["goals", "n"]].sum()
        m["rate"] = m["rate"].fillna(m["dbin"].map(tot["goals"] / tot["n"]))
        m["rate"] = m["rate"].fillna(table["goals"].sum() / table["n"].sum())
    xg = np.where(s["empty_net"].astype(bool).to_numpy(), np.nan, m["rate"].to_numpy())
    return pd.Series(xg, index=shots.index)


def load_table() -> pd.DataFrame:
    return pd.read_csv(DATA / "xg_table.csv")


# --- aggregates ---------------------------------------------------------------------

def danger_threshold(table: pd.DataFrame) -> float:
    """'Dangerous shot' = an attempt worth at least this much xG: the top fifth of all
    non-empty-net attempts in the fit season. Measured, not picked."""
    t = table.sort_values("rate")
    cum = t["n"].cumsum() / t["n"].sum()
    return float(t.loc[cum >= 0.80, "rate"].iloc[0])


def player_game_xg(shots: pd.DataFrame, hd: float) -> pd.DataFrame:
    """Per shooter per game: attempts, xG, dangerous attempts (empty nets excluded)."""
    s = shots[~shots["empty_net"].astype(bool) & shots["shooter_id"].notna()].copy()
    s["hd"] = (s["xg"] >= hd).astype(int)
    s["pp_xg"] = s["xg"].where(s["strength"] == "PP", 0.0)
    g = s.groupby(["shooter_id", "game_id"]).agg(
        attempts=("xg", "size"), ixg=("xg", "sum"), hd_attempts=("hd", "sum"),
        pp_ixg=("pp_xg", "sum")).reset_index().rename(columns={"shooter_id": "player_id"})
    g["player_id"] = g["player_id"].astype("int64")
    return g


def team_game_xg(shots: pd.DataFrame, hd: float) -> pd.DataFrame:
    """Per team per game: xG for and against, dangerous attempts for and against."""
    s = shots[~shots["empty_net"].astype(bool)].copy()
    s["hd"] = (s["xg"] >= hd).astype(int)
    f = s.groupby(["game_id", "team_id"]).agg(xgf=("xg", "sum"), hdf=("hd", "sum"),
                                             attempts_for=("xg", "size")).reset_index()
    teams = s.groupby("game_id")["team_id"].unique()
    rows = []
    for _, r in f.iterrows():
        other = [t for t in teams[r["game_id"]] if t != r["team_id"]]
        rows.append(other[0] if other else None)
    f["opp_id"] = rows
    a = f[["game_id", "team_id", "xgf", "hdf", "attempts_for"]].rename(
        columns={"team_id": "opp_id", "xgf": "xga", "hdf": "hda",
                 "attempts_for": "attempts_against"})
    return f.merge(a, on=["game_id", "opp_id"], how="left").drop(columns="opp_id")


# --- rates for the daily pipeline -----------------------------------------------------

PREV_FLOOR = 0.15      # same fade as scoring.player_rates: last season never < 15%
TEAM_PRIOR_GP = 10     # same as teams.PRIOR_GP
RECENT = 10


def load_model() -> tuple[pd.DataFrame, float, pd.DataFrame, pd.DataFrame]:
    """(table, dangerous-shot threshold, last season's player totals, team totals)."""
    meta = dict(line.split("=", 1) for line in (DATA / "xg_meta.txt").read_text().split())
    return (pd.read_csv(DATA / "xg_table.csv"), float(meta["danger_threshold"]),
            pd.read_csv(DATA / "xg_prev_players.csv"), pd.read_csv(DATA / "xg_prev_teams.csv"))


def player_xg_rates(cur_pg: pd.DataFrame, prev_players: pd.DataFrame,
                    skater_logs: pd.DataFrame, season: int, prev_season: int,
                    day: str) -> pd.DataFrame:
    """Per skater: expected goals per 60 and dangerous attempts per game, this season
    pulled toward last season while the sample is small."""
    logs = skater_logs[skater_logs["game_date"] < day]
    cur_l = logs[logs["season"] == season].groupby("player_id").agg(
        gp=("toi", "size"), toi=("toi", "sum"))
    prev_l = logs[logs["season"] == prev_season].groupby("player_id").agg(
        gp_p=("toi", "size"), toi_p=("toi", "sum"))
    cur = cur_pg.groupby("player_id")[["ixg", "hd_attempts", "pp_ixg"]].sum() \
        if not cur_pg.empty else pd.DataFrame(columns=["ixg", "hd_attempts", "pp_ixg"])
    prv = prev_players.set_index("player_id")[["ixg", "hd_attempts"]].add_suffix("_p")
    df = cur_l.join(prev_l, how="outer").join(cur, how="left").join(prv, how="left")
    df = df.fillna(0.0)
    lam = (1 - df["gp"] / 41).clip(lower=PREV_FLOOR)
    toi = df["toi"] + lam * df["toi_p"]
    gp = df["gp"] + lam * df["gp_p"]
    out = pd.DataFrame(index=df.index)
    out["ixg60"] = (60 * (df["ixg"] + lam * df["ixg_p"]) / toi.where(toi > 30))
    out["hd_pg"] = (df["hd_attempts"] + lam * df["hd_attempts_p"]) / gp.where(gp > 0)
    # last 10 of THIS season only: per-game shot data for last season is not shipped
    if not cur_pg.empty:
        last = (cur_pg.merge(logs[logs["season"] == season][["player_id", "game_id", "game_date"]],
                             on=["player_id", "game_id"], how="inner")
                .sort_values("game_date").groupby("player_id").tail(RECENT))
        rec = last.groupby("player_id").agg(ixg_recent=("ixg", "sum"),
                                            hd_recent=("hd_attempts", "sum"))
        out = out.join(rec, how="left")
    out.index.name = "player_id"
    return out.reset_index()


def team_xg_table(cur_tg: pd.DataFrame, prev_teams: pd.DataFrame, team_logs: pd.DataFrame,
                  teams: pd.DataFrame, season: int, day: str) -> pd.DataFrame:
    """Per team: xG for/against per game — this season (display), last 10 (display) and
    blended with last season (signals, suffix _b)."""
    cur_games = team_logs[(team_logs["season"] == season) & (team_logs["game_date"] < day)]
    t = cur_tg.merge(cur_games[["game_id", "team_id", "game_date"]],
                     on=["game_id", "team_id"], how="inner") if not cur_tg.empty else \
        pd.DataFrame(columns=["team_id", "game_date", "xgf", "xga", "hdf", "hda"])
    rows = []
    prev = prev_teams.set_index("team_id")
    for tid in teams["team_id"]:
        g = t[t["team_id"] == tid].sort_values("game_date")
        gp = len(g)
        r = {"team_id": tid, "xg_gp": gp}
        if gp:
            r.update(xgf_pg=g["xgf"].mean(), xga_pg=g["xga"].mean(),
                     hdf_pg=g["hdf"].mean(), hda_pg=g["hda"].mean(),
                     l10_xgf_pg=g["xgf"].tail(RECENT).mean(),
                     l10_xga_pg=g["xga"].tail(RECENT).mean())
        p = prev.loc[tid] if tid in prev.index else None
        wc = gp / (gp + TEAM_PRIOR_GP) if p is not None else 1.0
        for k in ("xgf", "xga", "hdf", "hda"):
            cv = r.get(f"{k}_pg")
            pv = (p[k] / p["games"]) if p is not None and p["games"] else None
            if cv is None and pv is None:
                continue
            r[f"{k}_pg_b"] = pv if cv is None else cv if pv is None else wc * cv + (1 - wc) * pv
        rows.append(r)
    out = pd.DataFrame(rows)
    out["xg_diff_pg_b"] = out.get("xgf_pg_b") - out.get("xga_pg_b")
    return out
