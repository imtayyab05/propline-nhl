"""Trend features (order 2, Oct 2026): track records, not predictions.

Everything here is a COUNT of what already happened - "2+ shots in 8 of last 10",
"points in 6 straight" - written in the terms of the client's book (Island Luck):
1+/2+ points, anytime/2+ goals, 1+ assists, shot lines. Nothing is a percentage chance,
and nothing feeds the board scores: the boards stay the ranking, these sit beside them
as evidence. Rare events (2+ goals) and tiny samples (head-to-head) are labelled
information only.

Built only from game logs the pipeline already collects - this season and last season
in full (with ice time), plus two older seasons of scoring lines for head-to-head.
"""

from __future__ import annotations

import math

import pandas as pd

SOG_LEVELS = (1, 2, 3, 4, 5)
SAFE_SHARE = 0.8          # "safe" = cleared in 8+ of last 10
AGGRESSIVE_SHARE = 0.4    # "aggressive" = above the safe line, cleared about half the time (4+ of 10)
MIN_GAMES = 5             # below this the last-10 window is too thin to call a line
SEASON_MIN_GP = 10        # until this season has 10 games, "season" means last season
HOT, COLD = 1.5, 0.5      # last 5 vs season rate that earns a hot / cold tag


def _pos_group(p) -> str:
    return "D" if p == "D" else "C" if p == "C" else "W"


def _count(values, k) -> int:
    return int(sum(1 for v in values if v >= k))


def _streak(values, k=1) -> int:
    """Consecutive most-recent games with value >= k (values oldest -> newest)."""
    n = 0
    for v in reversed(values):
        if v >= k:
            n += 1
        else:
            break
    return n


def _drought(values, k=1) -> int:
    """Consecutive most-recent games with value < k."""
    n = 0
    for v in reversed(values):
        if v < k:
            n += 1
        else:
            break
    return n


def _line(shots, share) -> tuple[int | None, int, int]:
    """Highest shot level cleared in at least `share` of these games.
    Returns (level, times cleared, games); level None if the sample is too thin."""
    n = len(shots)
    if n < MIN_GAMES:
        return None, 0, n
    need = math.ceil(share * n)
    best = None
    for k in SOG_LEVELS:
        if _count(shots, k) >= need:
            best = k
    if best is None:
        return None, 0, n
    return best, _count(shots, best), n


def _rate(v, games) -> float | None:
    return round(sum(v) / games, 2) if games else None


def player_trends(players: pd.DataFrame, logs: pd.DataFrame, history: pd.DataFrame,
                  season: int, prev_season: int, day: str) -> pd.DataFrame:
    """One row per player on tonight's boards.

    players: player_id, player_name, team, opponent, position (the scored skaters).
    logs:    this season + last season skater logs (propline/nhl.SKATER_LOG_COLS).
    history: older seasons' scoring lines (nhl.HISTORY_COLS), for head-to-head only.
    """
    base = logs[logs["game_date"] < day].sort_values("game_date")
    by_player = {pid: g for pid, g in base.groupby("player_id")}
    hist = pd.concat([history, base[history.columns.intersection(base.columns)]],
                     ignore_index=True) if history is not None and not history.empty else base
    hist = hist[hist["game_date"] < day]
    rows = []
    for p in players.itertuples():
        g = by_player.get(p.player_id)
        if g is None or g.empty:
            continue
        shots, pts, goals, ast = (list(g["shots"]), list(g["points"]), list(g["goals"]),
                                  list(g["assists"]))
        cur = g[g["season"] == season]
        prev = g[g["season"] == prev_season]
        # "season" = this season once it has 10 games; before that, last season, and
        # the row says which (early October would otherwise compare 5 games with 4).
        sea = cur if len(cur) >= SEASON_MIN_GP or prev.empty else prev
        basis = str(season)[:4] + "-" + str(season)[6:] if sea is cur else \
            str(prev_season)[:4] + "-" + str(prev_season)[6:]
        l10, l20, l5 = shots[-10:], shots[-20:], shots[-5:]

        r = {"player_id": p.player_id, "season_basis": basis, "games_logged": len(g)}

        # --- shot lines -------------------------------------------------------------
        safe, s_hit, s_n = _line(l10, SAFE_SHARE)
        aggr, a_hit, a_n = _line(l10, AGGRESSIVE_SHARE)
        if aggr is not None and safe is not None and aggr <= safe:
            # nothing above the safe line is cleared half the time: no separate call
            aggr = None
        # no level cleared in 8 of 10: say what the record IS, not a blank
        r["safe_sog"] = (f"{safe}+ ({s_hit}/{s_n})" if safe else
                         f"none: 1+ in {_count(l10, 1)}/{len(l10)}" if len(l10) >= MIN_GAMES
                         else f"too few games ({len(l10)})")
        r["aggressive_sog"] = f"{aggr}+ ({a_hit}/{a_n})" if aggr else None
        r["safe_level"], r["aggressive_level"] = safe, aggr
        for k in (1, 2, 3, 4):
            r[f"sog{k}_l10"] = f"{_count(l10, k)}/{len(l10)}"
            r[f"sog{k}_l20"] = f"{_count(l20, k)}/{len(l20)}"
            r[f"sog{k}_season"] = f"{_count(list(sea['shots']), k)}/{len(sea)}"

        # --- points, goals, assists in his book's terms ------------------------------
        r["pts1_l10"] = f"{_count(pts[-10:], 1)}/{len(pts[-10:])}"
        r["pts2_l10"] = f"{_count(pts[-10:], 2)}/{len(pts[-10:])}"
        r["pts2_l20"] = f"{_count(pts[-20:], 2)}/{len(pts[-20:])}"
        r["pts2_season"] = f"{_count(list(sea['points']), 2)}/{len(sea)}"
        r["pts2_count_l20"] = _count(pts[-20:], 2)
        r["goal1_l10"] = f"{_count(goals[-10:], 1)}/{len(goals[-10:])}"
        r["goal1_l20"] = f"{_count(goals[-20:], 1)}/{len(goals[-20:])}"
        r["goal2_l20"] = f"{_count(goals[-20:], 2)}/{len(goals[-20:])}"
        r["goal2_season"] = f"{_count(list(sea['goals']), 2)}/{len(sea)}"
        prev_g = list(prev["goals"])
        r["goal2_last_season"] = f"{_count(prev_g, 2)}/{len(prev_g)}" if prev_g else None
        r["goal2_count"] = _count(goals, 2)          # across both seasons logged
        r["ast1_l10"] = f"{_count(ast[-10:], 1)}/{len(ast[-10:])}"
        r["ast1_l20"] = f"{_count(ast[-20:], 1)}/{len(ast[-20:])}"

        # --- streaks and form ---------------------------------------------------------
        r["point_streak"] = _streak(pts)
        r["goal_streak"] = _streak(goals)
        r["sog3_streak"] = _streak(shots, 3)
        r["pointless"] = _drought(pts)
        r["goalless"] = _drought(goals)
        sea_n = len(sea)
        for name, vals, col in (("pts", pts, "points"), ("sog", shots, "shots"),
                                ("goals", goals, "goals")):
            l5v = vals[-5:]
            r[f"{name}_l5"] = _rate(l5v, len(l5v))
            r[f"{name}_season"] = _rate(list(sea[col]), sea_n)
        r["form"] = _form(r)

        # --- head-to-head vs tonight's opponent (information only) ---------------------
        h = hist[(hist["player_id"] == p.player_id) & (hist["opponent"] == p.opponent)]
        n = len(h)
        r["h2h_gp"] = n
        if n:
            r["h2h_goals"], r["h2h_assists"] = int(h["goals"].sum()), int(h["assists"].sum())
            r["h2h_points"] = int(h["points"].sum())
            r["h2h_sog_pg"] = round(h["shots"].mean(), 2)
            r["h2h_pts1"] = f"{_count(list(h['points']), 1)}/{n}"
            first = int(h["season"].min())
            r["h2h_seasons"] = f"since {str(first)[:4]}-{str(first)[6:]}"
            r["h2h_summary"] = (f"vs {p.opponent}: {n} GP, {r['h2h_goals']} G, "
                                f"{r['h2h_points']} P, {r['h2h_sog_pg']} SOG/G")
        rows.append(r)
    return pd.DataFrame(rows)


def _form(r: dict) -> str:
    """Short hot/cold note from streaks and last 5 vs season. Empty when nothing stands out."""
    notes = []
    if r["point_streak"] >= 3:
        notes.append(f"points in {r['point_streak']} straight")
    if r["goal_streak"] >= 2:
        notes.append(f"goal in {r['goal_streak']} straight")
    if r["sog3_streak"] >= 4:
        notes.append(f"3+ SOG in {r['sog3_streak']} straight")
    for name, label in (("pts", "pts"), ("sog", "SOG"), ("goals", "goals")):
        l5, sea = r.get(f"{name}_l5"), r.get(f"{name}_season")
        floor = {"pts": 0.5, "sog": 2.0, "goals": 0.25}[name]
        if l5 is None or not sea or sea < floor:
            continue
        if l5 >= HOT * sea:
            notes.append(f"hot: {l5} {label}/g last 5 vs {sea} season")
        elif l5 <= COLD * sea:
            notes.append(f"cold: {l5} {label}/g last 5 vs {sea} season")
    if r["pointless"] >= 4 and (r.get("pts_season") or 0) >= 0.6:
        notes.append(f"no point in {r['pointless']}")
    if r["goalless"] >= 8 and (r.get("goals_season") or 0) >= 0.3:
        notes.append(f"no goal in {r['goalless']}")
    return "; ".join(notes)


# --- DvP: what each team allows to centres, wingers and defence --------------------------

DVP_STATS = (("shots", "sog"), ("points", "pts"), ("goals", "g"))


def dvp_table(logs: pd.DataFrame, season: int, prev_season: int, day: str) -> pd.DataFrame:
    """Per defending team: SOG, points and goals allowed per game to C, W and D -
    this season, its last 10 games, and last season for context - with a rank
    (1 = allows the most). Built from the opponent column of the skater logs."""
    lg = logs[logs["game_date"] < day].copy()
    if lg.empty:
        return pd.DataFrame()
    lg["pg"] = lg["position"].map(_pos_group)
    per_game = (lg.groupby(["season", "opponent", "game_id", "game_date", "pg"])
                [["shots", "points", "goals"]].sum().reset_index())
    out = {}
    for label, frame in (("season", per_game[per_game["season"] == season]),
                         ("last", per_game[per_game["season"] == prev_season])):
        agg = frame.groupby(["opponent", "pg"])[["shots", "points", "goals"]].mean()
        for (team, pg), v in agg.iterrows():
            o = out.setdefault(team, {"team": team})
            for col, short in DVP_STATS:
                o[f"dvp_{pg}_{short}_{label}"] = round(v[col], 2)
        gp = frame.groupby("opponent")["game_id"].nunique()
        for team, n in gp.items():
            out.setdefault(team, {"team": team})[f"dvp_gp_{label}"] = int(n)
    # last 10 games of THIS season per defending team
    cur = per_game[per_game["season"] == season]
    for team, g in cur.groupby("opponent"):
        last_ids = (g.drop_duplicates("game_id").sort_values("game_date")
                    ["game_id"].tail(10))
        agg = g[g["game_id"].isin(last_ids)].groupby("pg")[["shots", "points", "goals"]].mean()
        for pg, v in agg.iterrows():
            for col, short in DVP_STATS:
                out[team][f"dvp_{pg}_{short}_l10"] = round(v[col], 2)
    df = pd.DataFrame(list(out.values()))
    for pg in ("C", "W", "D"):
        for _, short in DVP_STATS:
            for label in ("season", "l10", "last"):
                c = f"dvp_{pg}_{short}_{label}"
                if c in df:
                    df[c + "_rank"] = df[c].rank(ascending=False, method="min").astype("Int64")
    return df


def attach_dvp(trends: pd.DataFrame, players: pd.DataFrame, dvp: pd.DataFrame) -> pd.DataFrame:
    """A one-line DvP note on each player row: what tonight's opponent allows to his
    position this season, with its rank among 32 teams (1 = allows the most)."""
    if trends.empty or dvp is None or dvp.empty:
        return trends
    d = dvp.set_index("team")
    pos = dict(zip(players["player_id"], players["position"].map(_pos_group)))
    opp = dict(zip(players["player_id"], players["opponent"]))
    notes, ranks = [], []
    names = {"C": "all centres", "W": "all wingers", "D": "all defence"}
    for pid in trends["player_id"]:
        pg, o = pos.get(pid), opp.get(pid)
        # Until a team has 10 games this season, its DvP is a 2-5 game sample (Colorado
        # "allowed 4.0 SOG a game to centres" over 2 games on 7 Oct 2026, against a
        # league norm near 9.5). Use last season until then, and say which.
        gp = d.at[o, "dvp_gp_season"] if o in d.index and "dvp_gp_season" in d else 0
        which = "season" if (gp if pd.notna(gp) else 0) >= 10 else "last"
        col = f"dvp_{pg}_pts_{which}"
        if o not in d.index or col not in d or pd.isna(d.at[o, col]):
            notes.append(None)
            ranks.append(None)
            continue
        rk = d.at[o, col + "_rank"]
        when = "this season" if which == "season" else "last season"
        # totals per game to every player at that position combined, not per player
        notes.append(f"{o} allow {d.at[o, col]} pts and {d.at[o, f'dvp_{pg}_sog_{which}']} "
                     f"SOG a game to {names[pg]} combined, {when} "
                     f"(#{rk} of {len(d)} for points)")
        ranks.append(rk)
    out = trends.copy()
    out["dvp_note"], out["dvp_pts_rank"] = notes, ranks
    return out


# --- the trend tabs ---------------------------------------------------------------------

# Each trend tab lists the players of a parent board, in that board's order (the model
# still decides who is best set up), with the matching track record beside them.
TREND_BOARDS = {
    "sog_lines": "sog",        # safe and aggressive shot lines
    "multi_point": "points",   # 2+ point spots
    "multi_goal": "goals",     # 2+ goals: rare, information only
    "streaks": "points",       # hot and cold
    "h2h": "points",           # head-to-head: tiny samples, information only
}


def build_trend_boards(scores: pd.DataFrame) -> pd.DataFrame:
    """Long frame of trend-tab rows (prop = the tab), from the scored player boards."""
    frames = []
    for prop, parent in TREND_BOARDS.items():
        g = scores[scores["prop"] == parent].copy()
        if g.empty:
            continue
        g["prop"] = prop
        g["rationale"] = None          # the Why text belongs to the parent board
        g["rationale_fp"] = None
        if prop == "h2h":
            g = g[g["h2h_gp"].fillna(0) > 0]
        if prop == "streaks":
            # this tab opens on the hottest runs, the board score breaking ties
            g = g.sort_values(["point_streak", "sog3_streak", "goal_streak", "score"],
                              ascending=False)
            g["rank"] = range(1, len(g) + 1)
        frames.append(g)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
