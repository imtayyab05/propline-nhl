"""Shape pipeline output into database rows and push to Supabase.

Kept separate from db.py so that module stays a thin generic client: this file owns
the mapping between our DataFrames and the schema in db/schema.sql.
"""

from __future__ import annotations

import pandas as pd

from .db import check_json, delete_where, read, upsert

# Stored alongside each pick so the dashboard can show the "why" without recomputing.
# Per prop, matching the Excel tabs. The internal exp_* signals are NOT published.
PROP_DETAIL = {
    "sog": ["sog_pg_recent", "shots60", "opp_sa_pos", "hd_pg"],
    "goals": ["goalie_check", "goals_recent", "goals60", "sh_pct", "ixg60", "hd_pg", "sog_pg_recent",
              "pp_toi_recent",
              "opp_goalie", "opp_goalie_sv", "opp_goalie_status"],
    "assists": ["goalie_check", "assists_recent", "assists60", "pp_toi_recent", "team_gf", "opp_ga"],
    "points": ["goalie_check", "points_pg_recent", "points60", "ixg60", "pp_toi_recent", "opp_ga",
               "opp_goalie", "opp_goalie_sv", "opp_goalie_status"],
    "ppp": ["goalie_check", "ppp_recent", "ppp_pg", "pp_toi_recent", "pp_toi_bump", "opp_pen_taken",
            "opp_pk_pct"],
}
# rationale_fp: fingerprint of the inputs the "Why" text was written from, so the next
# Groq run can reuse the text when they are unchanged (propline/rationale.py).
COMMON_DETAIL = ["rationale_fp", "position", "recent_games", "gp_season", "toi_recent", "toi_bump",
                 "bump_from", "opp_missing", "game_id"]

GAME_DETAIL = ["rationale_fp", "home_team", "away_team", "home_gf_pg", "away_gf_pg", "home_ga_pg",
               "away_ga_pg", "home_goalie", "away_goalie", "home_goalie_sv",
               "away_goalie_sv", "goalies_lean", "combined_pp_threat",
               "missing_regulars", "missing_names", "market_total", "over_price",
               "under_price", "combined_xg", "goalie_check"]

# Phase 2 boards. Team boards carry `team` so the dashboard's team filter finds them;
# every board carries home_team / away_team for the same reason.
BOARD_DETAIL = {
    "team_goals": ["goalie_check", "team", "opponent", "gf", "xgf", "opp_ga", "opp_xga", "opp_goalie",
                   "opp_goalie_sv",
                   "pp_threat", "l10_gf", "missing_names", "market_team_total",
                   "tt_over_price", "tt_under_price"],
    "team_sog": ["team", "opponent", "sf", "opp_sa", "l10_sf", "opp_l10_sa",
                 "missing_names"],
    "team_ppg": ["goalie_check", "team", "opponent", "ppg", "pp_pct", "opp_pen_taken", "opp_pk_pct",
                 "opp_ppga"],
    "game_sog": ["pace", "l10_pace", "home_sf", "away_sf", "home_sa", "away_sa"],
    "game_ppg": ["goalie_check", "pp_threat_sum", "ppg_sum", "ppga_sum", "home_pp_pct", "away_pp_pct",
                 "home_opp_pen_taken", "away_opp_pen_taken"],
    "moneyline": ["goalie_check", "pick", "dog", "edge", "gap", "pick_goalie", "dog_goalie", "pick_b2b",
                  "dog_b2b", "home_missing_names", "away_missing_names", "pick_ml",
                  "dog_ml", "market_fav", "market_agrees"],
    "puck_line": ["goalie_check", "pick", "dog", "edge", "gap", "margin_fuel", "dog_goalie", "pick_ml",
                  "market_fav", "pick_pl_point", "pick_pl_price"],
}
BOARD_COMMON = ["rationale_fp", "home_team", "away_team"]

TEAM_COLS = ["team_id", "team", "team_name", "power_rank", "power_score", "gp", "record",
             "points", "points_pct", "gf_pg", "ga_pg", "sf_pg", "sa_pg", "ppg_pg",
             "ppga_pg", "pp_pct", "pk_pct", "pen_taken_pg", "l10_gp", "l10_record",
             "l10_gf_pg", "l10_ga_pg", "l10_sf_pg", "l10_sa_pg", "l10_ppg_pg",
             "l10_ppga_pg", "l10_pp_pct", "l10_pk_pct", "l10_pen_taken_pg"]
TEAM_DETAIL = ["gf_pg_b", "ga_pg_b", "sf_pg_b", "sa_pg_b", "pp_pct_b", "pk_pct_b",
               "goal_diff_pg_b", "shot_diff_pg_b", "l10_goal_diff_pg", "current_weight",
               # shot quality rides in details: no schema change needed
               "xgf_pg", "xga_pg", "l10_xgf_pg", "l10_xga_pg", "xgf_pg_b", "xga_pg_b",
               "xg_diff_pg_b", "xg_gp"]

# Columns the database types as bigint/int. Pandas widens any column containing a
# missing value to float, so an id arrives as "8478048.0" and Postgres rejects it.
INT_COLS = {
    "game_id", "team_id", "home_team_id", "away_team_id", "player_id", "subject_id",
    "goalie_id", "backup_id", "rank", "power_rank", "gp", "l10_gp", "points",
    "starts_recent", "games_missed", "game_type", "price", "books",
}


def _ints(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for c in df.columns:
        if c in INT_COLS:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("Int64")
    return df


def _clean(v):
    """One detail value, made JSON-safe. Containers first: pd.isna on a list is an
    array and `if` on it raises."""
    if isinstance(v, (list, dict)):
        return v
    if pd.isna(v):
        return None
    return v.item() if hasattr(v, "item") else v


def _details(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    present = [c for c in cols if c in df.columns]
    return df[present].apply(lambda r: {k: _clean(v) for k, v in r.items()}, axis=1)


def _keep_rationale(table: str, df: pd.DataFrame, day: str, keys: list[str]) -> pd.DataFrame:
    """Carry the Why text forward from the slate's earlier run when this run wrote none.

    Groq runs only on the morning and pre-game slots (its quota is shared with MLB), and
    every publish replaces the whole board, so the midday run on 4 Oct 2026 wiped the
    morning's sentences. A sentence cites season and last-10 numbers that do not move
    during the day, so reusing it until the next Groq run is safe; a fresh one always
    wins over a carried one.
    """
    if "rationale" not in df or df["rationale"].notna().all():
        return df
    # The fingerprint travels with the text. Without it the next Groq run would see a
    # sentence with no fingerprint and pay to rewrite every carried pick.
    prev = read(table, {"select": ",".join(keys + ["rationale"]) + ",_oldfp:details->>rationale_fp",
                        "slate_date": f"eq.{day}", "rationale": "not.is.null",
                        "limit": "5000"})
    if not prev:
        return df
    old = pd.DataFrame(prev).rename(columns={"rationale": "_old"})
    out = df.merge(old, on=keys, how="left")
    carried = out["rationale"].isna() & out["_old"].notna()
    out["rationale"] = out["rationale"].where(~carried, out["_old"])
    if "details" in out:
        out["details"] = [
            {**(d or {}), "rationale_fp": fp} if c and fp else d
            for d, c, fp in zip(out["details"], carried, out["_oldfp"])]
    return out.drop(columns=["_old", "_oldfp"])


TEAM_BOARDS = ("team_goals", "team_sog", "team_ppg")


def previous_rationales(slate_date) -> dict:
    """Explanations already published for this slate, for add_rationales to reuse.

    Keyed the way process.py keys its picks: (prop, player_id) for player props,
    (prop, game_id, team) for the team boards, (prop, game_id) for every other game
    board - each with the fingerprint of the inputs its sentence was written from.
    Only rows that HAVE text are fetched (~150), well under PostgREST's 1,000-row cap.
    Failure here just means everything is written afresh.
    """
    out: dict = {}
    try:
        for r in read("prop_picks", {
                "select": "prop,subject_id,rationale,fp:details->>rationale_fp",
                "slate_date": f"eq.{slate_date}", "rationale": "not.is.null",
                "limit": "1000"}):
            if r.get("fp") and r.get("subject_id") is not None:
                out[(r["prop"], int(r["subject_id"]))] = (r["fp"], r["rationale"])
        for r in read("game_picks", {
                "select": "prop,game_id,subject,rationale,fp:details->>rationale_fp",
                "slate_date": f"eq.{slate_date}", "rationale": "not.is.null",
                "limit": "1000"}):
            if not r.get("fp") or r.get("game_id") is None:
                continue
            key = ((r["prop"], int(r["game_id"]), r["subject"]) if r["prop"] in TEAM_BOARDS
                   else (r["prop"], int(r["game_id"])))
            out[key] = (r["fp"], r["rationale"])
    except Exception as exc:  # noqa: BLE001 - reuse is an optimisation, never a blocker
        print(f"  WARN  could not read earlier explanations ({exc}) - writing all afresh")
    return out


def _snapshot(table: str, df: pd.DataFrame, filters: dict, on_conflict: str) -> int:
    """Replace one slate's rows. Serialisation is proven BEFORE the delete, so a bad
    payload fails loudly with yesterday's board intact rather than an empty one."""
    df = _ints(df)
    check_json(df)
    delete_where(table, filters)
    return upsert(table, df, on_conflict=on_conflict)


def publish_lines(slate_date, lines: pd.DataFrame) -> int:
    """Upsert market lines. Never cleared: a game that has started keeps the last
    pre-game line it had, because in-play lines are not fetched (see odds.fetch)."""
    if lines is None or lines.empty:
        return 0
    rows = lines.copy()
    rows.insert(0, "slate_date", str(slate_date))
    rows = _ints(rows)
    check_json(rows)
    return upsert("market_lines", rows,
                  on_conflict="slate_date,game_id,market,subject,side")


def read_lines(slate_date) -> pd.DataFrame:
    rows = read("market_lines", {"select": "*", "slate_date": f"eq.{slate_date}",
                                 "limit": "5000"})
    return pd.DataFrame(rows)


def _board_rows(day: str, prop: str, g: pd.DataFrame) -> pd.DataFrame:
    if prop in ("team_goals", "team_sog", "team_ppg"):
        subject, status = g["team"], g["opp_goalie_status"]
    elif prop == "puck_line":
        subject = g["subject"]
        status = g["away_goalie_status"].astype(str) + " / " + g["home_goalie_status"].astype(str)
    else:
        subject = g["matchup"]
        status = g["away_goalie_status"].astype(str) + " / " + g["home_goalie_status"].astype(str)
    return pd.DataFrame({
        "slate_date": day, "game_id": g["game_id"], "prop": prop, "subject": subject,
        "rank": g["rank"], "score": g["score"], "goalies_status": status,
        "rationale": g.get("rationale"),
        "details": _details(g, BOARD_DETAIL[prop] + BOARD_COMMON),
    })


def publish_slate(slate_date, schedule, starters, avail, player_scores, total_goals,
                  team_tbl, top_n: int | None = None,
                  game_boards: dict | None = None) -> dict[str, int]:
    """Write the whole slate. Returns rows written per table.

    top_n=None publishes every scored row: the dashboard can only filter what it has
    been given (MLB lesson — a top-40 cut made "Confirmed only" look empty).
    """
    written: dict[str, int] = {}
    day = str(slate_date)
    cut = (lambda d: d) if top_n is None else (lambda d: d.head(top_n))

    if schedule is not None and not schedule.empty:
        written["games"] = upsert("games", _ints(schedule), on_conflict="game_id")

    if starters is not None and not starters.empty:
        s = starters.copy()
        s["game_date"] = day
        cols = ["game_date", "game_id", "team_id", "team", "goalie_id", "goalie_name",
                "status", "confidence", "reason", "starts_recent", "back_to_back",
                "backup_id", "backup_name", "sv_pct"]
        written["goalie_starts"] = _snapshot(
            "goalie_starts", s[[c for c in cols if c in s]],
            {"game_date": f"eq.{day}"}, "game_id,team_id")

    if avail is not None and not avail.empty:
        o = avail[(avail["status"] == "out") & avail["regular"]].copy()
        o["game_date"] = day
        cols = ["game_date", "team", "player_id", "player_name", "position", "base_toi",
                "games_missed", "status", "status_source", "reason"]
        if not o.empty:
            written["player_status"] = _snapshot(
                "player_status", o[cols], {"game_date": f"eq.{day}"},
                "game_date,team,player_id")
        else:
            delete_where("player_status", {"game_date": f"eq.{day}"})

        pl = avail[["player_id", "player_name", "team", "position"]].drop_duplicates(
            "player_id")
        written["players"] = upsert("players", _ints(pl), on_conflict="player_id")

    if player_scores is not None and not player_scores.empty:
        picks = []
        for prop, grp in player_scores.groupby("prop"):
            g = cut(grp.sort_values("rank")).copy()
            picks.append(pd.DataFrame({
                "slate_date": day, "prop": prop,
                "subject_id": g["player_id"], "subject_name": g["player_name"],
                "team": g["team"], "opponent": g["opponent"],
                "rank": g["rank"], "score": g["score"],
                "lineup_status": g["lineup_status"],
                "rationale": g.get("rationale"),
                "details": _details(g, PROP_DETAIL.get(prop, []) + COMMON_DETAIL),
            }))
        allp = pd.concat(picks, ignore_index=True).drop_duplicates(
            subset=["slate_date", "prop", "subject_id"])
        allp = _keep_rationale("prop_picks", allp, day, ["prop", "subject_id"])
        written["prop_picks"] = _snapshot("prop_picks", allp, {"slate_date": f"eq.{day}"},
                                          "slate_date,prop,subject_id")

    frames = []
    if total_goals is not None and not total_goals.empty:
        g = total_goals.copy()
        frames.append(pd.DataFrame({
            "slate_date": day, "game_id": g["game_id"], "prop": "total_goals",
            "subject": g["matchup"], "rank": g["rank"], "score": g["score"],
            "goalies_status": g["goalies_status"], "rationale": g.get("rationale"),
            "details": _details(g, GAME_DETAIL),
        }))
    for prop, g in (game_boards or {}).items():
        if g is not None and not g.empty:
            frames.append(_board_rows(day, prop, g))
    if frames:
        rows = pd.concat(frames, ignore_index=True)
        rows = _keep_rationale("game_picks", rows, day, ["prop", "game_id", "subject"])
        written["game_picks"] = _snapshot("game_picks", rows, {"slate_date": f"eq.{day}"},
                                          "slate_date,prop,game_id,subject")

    if team_tbl is not None and not team_tbl.empty:
        t = team_tbl.copy()
        rows = t[[c for c in TEAM_COLS if c in t]].copy()
        rows.insert(0, "slate_date", day)
        rows["details"] = _details(t, TEAM_DETAIL)
        written["team_stats"] = _snapshot("team_stats", rows, {"slate_date": f"eq.{day}"},
                                          "slate_date,team_id")

    return written
