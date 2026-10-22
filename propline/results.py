"""Results / Track Record (order 3, Oct 2026): freeze each game's picks before puck drop,
grade them against the final box scores the next morning.

Nothing here changes a score or a rank. It only records what the boards said and what
happened.

WHY A FREEZE. prop_picks / game_picks are replaced on every run, and nothing in the
pipeline skips a game that has started: the late run (01:37 UTC) re-scores the early
games with information nobody had before puck drop (the boxscore's confirmed starter,
the posted game roster). So the stored boards cannot be graded fairly. Each publish
writes a snapshot of every game that has NOT started yet (replacing that game's
earlier snapshot), and never touches a game whose start time has passed. The table's
trigger (db/results.sql) refuses such writes too. What gets graded is therefore each
game's board as of its last run before puck drop.

RANK. A snapshot row keeps the rank it had in its own run. Games freeze at different
runs (an early game at the pre-game run, a late one at the late run), so within one
slate those ranks come from different runs. Grading re-ranks a board's frozen rows by
their frozen score (a 0-100 percentile within its run's slate), the frozen rank
breaking ties, so "top 5" is always exactly five picks.

GRADING RULES (how books settle these markets; written down for the guide):
  - Player props (goals, points, assists, PP points, shots): regulation + overtime;
    shootout attempts are not goals or shots. A player who did not play is VOID.
  - Goals board: 1+ goal (anytime). Multi-Goal: 2+ goals, same board order.
  - Points board: 1+ point. 2+ Points: 2+ points, same board order.
  - Assists: 1+ assist. PP Points: 1+ power-play point.
  - SOG board / SOG Lines: no market line exists for player shots in this project, so
    each player is graded against the lines the board shows: his safe line and,
    separately, his aggressive line (e.g. "3+"). No line = not graded.
  - Total Goals: only games that showed a MODEL LEAN badge, lean side vs the market
    total, final score including overtime and the shootout (the shootout winner is
    credited one goal, as in the official final score). Exact line = push = VOID.
  - Team Goals: tagged teams vs their tag (Over 2.5 / Over 3.5), and every team with a
    market team total vs that line (over), final score incl. OT and shootout.
  - Moneyline: the stronger side won, incl. OT and shootout.
  - Puck Line: the stronger side covered its puck line (the market's number when we
    have one, otherwise -1.5) on the final score incl. OT and shootout.
  - Game not completed (postponed, suspended) = VOID.
  - Not graded: Streaks, Head-to-Head and DvP (information tabs, no outcome), and the
    SOG / PPG game and team boards (no book offers a line for them). There is no
    saves board.

BASE RATE. Each graded row carries base_p, the rate for its board that day across
everyone listed: e.g. the share of ALL listed skaters on the Goals board who scored.
For the market boards it is what the market's side did: the share of games the market
favourite won (moneyline) or covered -1.5 (puck line), the share of lined games that
went the lean's way (total goals), the share of all teams that cleared the tag's number.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

import pandas as pd

from . import nhl
from .db import check_json, delete_where, read, upsert

PLAYER_BOARDS = ("sog", "points", "goals", "assists", "ppp")
GAME_BOARDS = ("total_goals", "team_goals", "moneyline", "puck_line")
KEEP_TOP = 25                  # graded rows kept per player board (the page's top 25)
FREEZE_MARGIN = timedelta(minutes=2)   # a game this close to puck drop is not rewritten
FINAL = {"OFF", "FINAL"}
PRUNE_DAYS = 21                # snapshots are only needed until graded

# graded board key -> (parent board, label, stat column, threshold)
PLAYER_RULES = [
    ("goals", "goals", "1+ goal", "goals", 1),
    ("multi_goal", "goals", "2+ goals", "goals", 2),
    ("points", "points", "1+ point", "points", 1),
    ("multi_point", "points", "2+ points", "points", 2),
    ("assists", "assists", "1+ assist", "assists", 1),
    ("ppp", "ppp", "1+ PP point", "pp_points", 1),
]
BOARD_LABELS = {
    "goals": "Goals (anytime)", "multi_goal": "Multi-Goal (2+)",
    "points": "Points (1+)", "multi_point": "2+ Points", "assists": "Assists (1+)",
    "ppp": "PP Points (1+)", "sog_safe": "SOG - safe line",
    "sog_aggressive": "SOG - aggressive line", "total_goals": "Total Goals - model lean",
    "team_goals_tag": "Team Goals - tag", "team_goals_total": "Team Goals - over market",
    "moneyline": "Moneyline - stronger side", "puck_line": "Puck Line - covered",
}


def _py(v):
    """JSON-safe scalar: numpy -> python, NaN -> None."""
    if v is None:
        return None
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _num(v):
    v = _py(v)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# --- freeze ---------------------------------------------------------------------------

def snapshot_rows(day: str, schedule: pd.DataFrame, player_scores: pd.DataFrame,
                  total_goals: pd.DataFrame, boards: dict, run_kind: str,
                  now: datetime | None = None) -> tuple[pd.DataFrame, list[int]]:
    """Rows for every game that has not started. Returns (rows, open game ids)."""
    now = now or datetime.now(timezone.utc)
    if schedule is None or schedule.empty:
        return pd.DataFrame(), []
    s = schedule.copy()
    s["start"] = pd.to_datetime(s["game_time_utc"], utc=True, errors="coerce")
    open_games = s[s["start"].notna() & (s["start"] > now + FREEZE_MARGIN)]
    if open_games.empty:
        return pd.DataFrame(), []
    start = dict(zip(open_games["game_id"].astype(int), open_games["start"]))
    team_game = {}
    for _, g in open_games.iterrows():
        team_game[g["home_team"]] = int(g["game_id"])
        team_game[g["away_team"]] = int(g["game_id"])

    rows = []

    def add(board, gid, subject, name, team, opp, rank, score, line=None, extra=None):
        rows.append({
            "slate_date": day, "board": board, "game_id": int(gid),
            "subject": str(subject), "subject_name": _py(name), "team": _py(team),
            "opponent": _py(opp), "rank": int(rank), "score": _num(score),
            "line": _num(line), "extra": {k: _py(v) for k, v in (extra or {}).items()},
            "game_start": start[int(gid)].isoformat(), "run_type": run_kind,
            "frozen_at": now.isoformat(),
        })

    if player_scores is not None and not player_scores.empty:
        ps = player_scores[player_scores["prop"].isin(PLAYER_BOARDS)]
        for _, r in ps.iterrows():
            gid = team_game.get(r["team"])
            if gid is None:
                continue
            if r["prop"] == "sog":
                add("sog", gid, r["player_id"], r["player_name"], r["team"], r["opponent"],
                    r["rank"], r["score"], r.get("safe_level"),
                    {"aggressive": r.get("aggressive_level")})
            else:
                add(r["prop"], gid, r["player_id"], r["player_name"], r["team"],
                    r["opponent"], r["rank"], r["score"])

    if total_goals is not None and not total_goals.empty:
        for _, r in total_goals.iterrows():
            if int(r["game_id"]) in start:
                add("total_goals", r["game_id"], "game", r["matchup"], r["away_team"],
                    r["home_team"], r["rank"], r["score"], r.get("market_total"),
                    {"lean": r.get("model_lean"), "proj": r.get("proj_total")})
    tg = (boards or {}).get("team_goals")
    if tg is not None and not tg.empty:
        for _, r in tg.iterrows():
            if int(r["game_id"]) in start:
                add("team_goals", r["game_id"], r["team"], r["team"], r["team"],
                    r["opponent"], r["rank"], r["score"], r.get("market_team_total"),
                    {"tag": r.get("tt_tag"), "proj": r.get("proj_goals")})
    for key in ("moneyline", "puck_line"):
        b = (boards or {}).get(key)
        if b is None or b.empty:
            continue
        for _, r in b.iterrows():
            if int(r["game_id"]) in start:
                add(key, r["game_id"], r["pick"], r.get("matchup"), r["pick"], r["dog"],
                    r["rank"], r["score"],
                    r.get("pick_pl_point") if key == "puck_line" else None,
                    {"market_fav": r.get("market_fav"), "edge": r.get("edge")})
    return pd.DataFrame(rows), sorted(start)


def freeze(day: str, rows: pd.DataFrame, open_ids: list[int]) -> int:
    """Replace the snapshot of the games that have not started; leave the rest alone."""
    if not open_ids or rows.empty:
        return 0
    check_json(rows)
    delete_where("pick_snapshots", {"slate_date": f"eq.{day}",
                                    "game_id": f"in.({','.join(map(str, open_ids))})"})
    return upsert("pick_snapshots", rows, on_conflict="slate_date,board,game_id,subject")


# --- grading --------------------------------------------------------------------------

def final_scores(game_ids) -> dict[int, dict]:
    """Final score per game from the official boxscore (includes the shootout goal)."""
    out = {}
    for gid in game_ids:
        try:
            b = nhl._get(f"{nhl.WEB}/gamecenter/{int(gid)}/boxscore")
        except Exception as exc:  # noqa: BLE001 - recorded, graded as not final
            out[int(gid)] = {"state": f"error: {exc}"[:60]}
            continue
        h, a = b.get("homeTeam", {}), b.get("awayTeam", {})
        period = ((b.get("gameOutcome") or {}).get("lastPeriodType")
                  or (b.get("periodDescriptor") or {}).get("periodType"))
        out[int(gid)] = {"state": b.get("gameState"), "home": h.get("abbrev"),
                         "away": a.get("abbrev"), "home_score": h.get("score"),
                         "away_score": a.get("score"), "period": period}
    return out


def _score_text(g: dict) -> str:
    tag = f" ({g['period']})" if g.get("period") in ("OT", "SO") else ""
    return f"{g['away']} {g['away_score']}-{g['home_score']} {g['home']}{tag}"


def _rerank(df: pd.DataFrame) -> pd.Series:
    order = df.sort_values(["score", "rank"], ascending=[False, True]).index
    return pd.Series(range(1, len(order) + 1), index=order).reindex(df.index)


def grade(day: str, snaps: pd.DataFrame, games: dict[int, dict],
          logs: pd.DataFrame) -> pd.DataFrame:
    """One row per graded pick. `logs` = skater game logs for the day (stats API)."""
    out = []
    snaps = snaps.copy()
    snaps["extra"] = snaps["extra"].apply(lambda x: x if isinstance(x, dict) else {})
    final = {g for g, v in games.items() if v.get("state") in FINAL}
    stat = {}
    if logs is not None and not logs.empty:
        for _, r in logs.iterrows():
            stat[(int(r["player_id"]), int(r["game_id"]))] = r

    def row(r, board, pick, line, actual, actual_text, result, note, rank):
        return {"slate_date": day, "board": board, "game_id": int(r["game_id"]),
                "subject": str(r["subject"]), "subject_name": r.get("subject_name"),
                "team": r.get("team"), "opponent": r.get("opponent"), "rank": int(rank),
                "frozen_rank": _py(r.get("rank")), "score": _num(r.get("score")),
                "pick": pick, "line": _num(line), "actual": _num(actual),
                "actual_text": actual_text, "result": result, "note": note,
                "frozen_at": r.get("frozen_at"), "run_type": r.get("run_type")}

    # --- player boards ----------------------------------------------------------------
    for parent in PLAYER_BOARDS:
        b = snaps[snaps["board"] == parent].copy()
        if b.empty:
            continue
        b["grank"] = _rerank(b)
        rules = [(k, lab, col, th) for k, p, lab, col, th in PLAYER_RULES if p == parent]
        if parent == "sog":
            rules = [("sog_safe", None, "shots", "line"),
                     ("sog_aggressive", None, "shots", "aggressive")]
        for key, label, col, th in rules:
            graded = []
            for _, r in b.iterrows():
                if th == "line":
                    need = _num(r.get("line"))
                elif th == "aggressive":
                    need = _num(r["extra"].get("aggressive"))
                else:
                    need = th
                if need is None:
                    continue                      # no line shown: not graded
                need = int(need)
                pick = label or f"{need}+ shots"
                gid = int(r["game_id"])
                if gid not in final:
                    graded.append(row(r, key, pick, need, None, None, "void",
                                      "game not completed", r["grank"]))
                    continue
                s = stat.get((int(r["subject"]), gid))
                if s is None:
                    graded.append(row(r, key, pick, need, None, "did not play", "void",
                                      "did not play", r["grank"]))
                    continue
                v = int(s[col])
                graded.append(row(r, key, pick, need, v, str(v),
                                  "hit" if v >= need else "miss", None, r["grank"]))
            if not graded:
                continue
            g = pd.DataFrame(graded)
            live = g[g["result"] != "void"]
            base = (live["result"] == "hit").mean() if len(live) else None
            g["base_p"] = base
            out.append(g[g["rank"] <= KEEP_TOP])

    # --- game boards ------------------------------------------------------------------
    def team_goals_of(gid, team):
        g = games[gid]
        return g["home_score"] if team == g["home"] else g["away_score"]

    tot = snaps[snaps["board"] == "total_goals"].copy()
    if not tot.empty:
        tot["grank"] = _rerank(tot)
        sides = []                                # every lined, completed, non-push game
        for _, r in tot.iterrows():
            gid, line = int(r["game_id"]), _num(r.get("line"))
            if gid in final and line is not None:
                t = games[gid]["home_score"] + games[gid]["away_score"]
                if t != line:
                    sides.append("over" if t > line else "under")
        rate = {s: (sides.count(s) / len(sides) if sides else None) for s in ("over", "under")}
        graded = []
        for _, r in tot.iterrows():
            lean, line = r["extra"].get("lean"), _num(r.get("line"))
            if not lean or line is None:
                continue
            side = "over" if "OVER" in lean else "under"
            pick = f"{side.title()} {line:g}"
            gid = int(r["game_id"])
            if gid not in final:
                graded.append(row(r, "total_goals", pick, line, None, None, "void",
                                  "game not completed", r["grank"]))
                continue
            g = games[gid]
            t = g["home_score"] + g["away_score"]
            res = "void" if t == line else (
                "hit" if (t > line) == (side == "over") else "miss")
            x = row(r, "total_goals", pick, line, t, _score_text(g), res,
                    "push" if res == "void" else None, r["grank"])
            x["base_p"] = rate[side]
            graded.append(x)
        if graded:
            out.append(pd.DataFrame(graded))

    tg = snaps[snaps["board"] == "team_goals"].copy()
    if not tg.empty:
        tg["grank"] = _rerank(tg)
        done = tg[tg["game_id"].astype(int).isin(final)]
        scored = [team_goals_of(int(r["game_id"]), r["subject"]) for _, r in done.iterrows()]
        tagged, totals = [], []
        lined = []
        for _, r in done.iterrows():
            line = _num(r.get("line"))
            if line is not None:
                v = team_goals_of(int(r["game_id"]), r["subject"])
                if v != line:
                    lined.append(v > line)
        over_rate = (sum(lined) / len(lined)) if lined else None
        for _, r in tg.iterrows():
            gid = int(r["game_id"])
            tag, line = r["extra"].get("tag"), _num(r.get("line"))
            g = games.get(gid, {})
            for key, pick_line, pick in (
                    ("team_goals_tag", float(tag.rsplit(" ", 1)[-1]) if tag else None,
                     tag.replace("Team Total ", "") if tag else None),
                    ("team_goals_total", line, f"Over {line:g}" if line is not None else None)):
                if pick_line is None:
                    continue
                if gid not in final:
                    x = row(r, key, pick, pick_line, None, None, "void",
                            "game not completed", r["grank"])
                else:
                    v = team_goals_of(gid, r["subject"])
                    res = "void" if v == pick_line else ("hit" if v > pick_line else "miss")
                    x = row(r, key, pick, pick_line, v, _score_text(g), res,
                            "push" if res == "void" else None, r["grank"])
                if key == "team_goals_tag":
                    x["base_p"] = (sum(s > pick_line for s in scored) / len(scored)
                                   if scored else None)
                    tagged.append(x)
                else:
                    x["base_p"] = over_rate
                    totals.append(x)
        for lst in (tagged, totals):
            if lst:
                out.append(pd.DataFrame(lst))

    for key in ("moneyline", "puck_line"):
        b = snaps[snaps["board"] == key].copy()
        if b.empty:
            continue
        b["grank"] = _rerank(b)
        # base: what the market favourite did on this slate
        fav_won, fav_cov = [], []
        for _, r in b.iterrows():
            gid, fav = int(r["game_id"]), r["extra"].get("market_fav")
            if gid in final and fav:
                g = games[gid]
                other = g["away"] if fav == g["home"] else g["home"]
                m = team_goals_of(gid, fav) - team_goals_of(gid, other)
                fav_won.append(m > 0)
                fav_cov.append(m >= 2)
        graded = []
        for _, r in b.iterrows():
            gid, pick, dog = int(r["game_id"]), r["subject"], r.get("opponent")
            point = _num(r.get("line")) if key == "puck_line" else None
            if key == "puck_line" and point is None:
                point = -1.5
            label = pick if key == "moneyline" else f"{pick} {point:+g}"
            if gid not in final:
                graded.append(row(r, key, label, point, None, None, "void",
                                  "game not completed", r["grank"]))
                continue
            g = games[gid]
            m = team_goals_of(gid, pick) - team_goals_of(gid, dog)
            if key == "moneyline":
                res = "hit" if m > 0 else "miss"
                base = sum(fav_won) / len(fav_won) if fav_won else None
            else:
                res = "void" if m + point == 0 else ("hit" if m + point > 0 else "miss")
                cov = sum(fav_cov) / len(fav_cov) if fav_cov else None
                base = None if cov is None else (cov if point < 0 else 1 - cov)
            x = row(r, key, label, point, m, _score_text(g), res, None, r["grank"])
            x["base_p"] = base
            graded.append(x)
        if graded:
            out.append(pd.DataFrame(graded))

    if not out:
        return pd.DataFrame()
    res = pd.concat(out, ignore_index=True)
    res["base_p"] = res["base_p"].astype(float).round(4)
    return res


# --- summary --------------------------------------------------------------------------

BUCKETS = (5, 10, 25)
PLAYER_KEYS = {"goals", "multi_goal", "points", "multi_point", "assists", "ppp", "sog_safe",
               "sog_aggressive"}
RANKED = {"team_goals_total", "moneyline", "puck_line"}


def summarise(res: pd.DataFrame) -> pd.DataFrame:
    """Hit rate by board and rank bucket, with the base rate beside it."""
    if res is None or res.empty:
        return pd.DataFrame()
    live = res[res["result"] != "void"]
    rows = []
    for board, g in live.groupby("board"):
        if board in PLAYER_KEYS:            # only the top 25 are kept
            cuts = [(f"Top {n}", g[g["rank"] <= n]) for n in BUCKETS]
        elif board in RANKED:
            cuts = [(f"Top {n}", g[g["rank"] <= n]) for n in BUCKETS[:2]] + [("All", g)]
        else:                               # leans and tags: every badge shown
            cuts = [("All", g)]
        for name, c in cuts:
            if c.empty:
                continue
            rows.append({"board": BOARD_LABELS.get(board, board), "bucket": name,
                         "picks": len(c), "hits": int((c["result"] == "hit").sum()),
                         "hit_rate": (c["result"] == "hit").mean(),
                         "base_rate": c["base_p"].mean()})
    return pd.DataFrame(rows)


# --- test source: the stored boards (NOT a fair record) ---------------------------------

def boards_as_snapshots(day: str) -> pd.DataFrame:
    """Build snapshot-shaped rows from the stored boards of a past slate. Only for testing
    the grader: these boards came from the slate's last run, which for most games was
    after puck drop, so they must never be written to pick_results."""
    games = read("games", {"select": "game_id,home_team,away_team,game_time_utc",
                           "game_date": f"eq.{day}"})
    team_game = {}
    for g in games:
        team_game[g["home_team"]] = g["game_id"]
        team_game[g["away_team"]] = g["game_id"]
    rows = []
    for prop in PLAYER_BOARDS:
        off = 0
        while True:
            page = read("prop_picks", {"select": "subject_id,subject_name,team,opponent,rank,"
                                       "score,details", "slate_date": f"eq.{day}",
                                       "prop": f"eq.{prop}", "limit": "1000",
                                       "offset": str(off)})
            for r in page:
                d = r.get("details") or {}
                line = extra = None
                if prop == "sog":
                    s, a = d.get("safe_sog") or "", d.get("aggressive_sog") or ""
                    line = int(s.split("+")[0]) if s[:1].isdigit() else None
                    extra = int(a.split("+")[0]) if a[:1].isdigit() else None
                rows.append({"board": prop, "game_id": team_game.get(r["team"]),
                             "subject": str(r["subject_id"]),
                             "subject_name": r["subject_name"], "team": r["team"],
                             "opponent": r["opponent"], "rank": r["rank"],
                             "score": r["score"], "line": line,
                             "extra": {"aggressive": extra}})
            if len(page) < 1000:
                break
            off += 1000
    for r in read("game_picks", {"select": "game_id,prop,subject,rank,score,details",
                                 "slate_date": f"eq.{day}", "limit": "1000"}):
        d, p = r.get("details") or {}, r["prop"]
        if p == "total_goals":
            rows.append({"board": p, "game_id": r["game_id"], "subject": "game",
                         "subject_name": r["subject"], "team": d.get("away_team"),
                         "opponent": d.get("home_team"), "rank": r["rank"],
                         "score": r["score"], "line": d.get("market_total"),
                         "extra": {"lean": d.get("model_lean")}})
        elif p == "team_goals":
            rows.append({"board": p, "game_id": r["game_id"], "subject": d.get("team"),
                         "subject_name": d.get("team"), "team": d.get("team"),
                         "opponent": d.get("opponent"), "rank": r["rank"],
                         "score": r["score"], "line": d.get("market_team_total"),
                         "extra": {"tag": d.get("tt_tag")}})
        elif p in ("moneyline", "puck_line"):
            rows.append({"board": p, "game_id": r["game_id"], "subject": d.get("pick"),
                         "subject_name": r["subject"], "team": d.get("pick"),
                         "opponent": d.get("dog"), "rank": r["rank"], "score": r["score"],
                         "line": d.get("pick_pl_point") if p == "puck_line" else None,
                         "extra": {"market_fav": d.get("market_fav")}})
    df = pd.DataFrame(rows)
    return df[df["game_id"].notna()] if not df.empty else df


def day_logs(day: str) -> pd.DataFrame:
    d = date.fromisoformat(day)
    return nhl.skater_game_logs(nhl.season_of(d), d, d)
