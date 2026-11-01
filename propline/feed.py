"""AI data feed (order 4, Nov 2026): one compact summary of tonight's slate.

Built at every run from the same data as the boards, stored in ai_feed and served
read-only at /api/feed (JSON, or Markdown for the dashboard's "Copy for AI" button; the
Markdown is rendered by netlify/functions/feed.mjs, which also overlays live game status
from the official feed).

Written for Devin's own Gemini model, which until now ingested the whole workbook plus
MoneyPuck CSVs, kept hitting its limits and filled gaps by inventing numbers. So:
  - every number here is computed by PropLine; nothing is left for the model to guess;
  - the header lists today's teams explicitly (his model has a strict today-only rule);
  - per player: L3 / L5 / L10 / season rates side by side, so momentum can be computed
    (his L5-to-season ratio) instead of estimated;
  - a "low-TOI efficiency" list built with his own gate (TOI L10 12.0-16.9 min and
    Pts/60 > 3.0 or Ast/60 > 2.0), and every goalie's adjusted SV% (his .888 rule);
  - no win probabilities, no staking, no betting advice: PropLine scores are percentile
    ranks within tonight's slate, and the feed says so at the top.

Size target: under ~4,000 tokens on a busy Saturday. Players appear once (a shared
table) and each board lists only ids, so a star on three boards costs one row.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
TOP_PER_BOARD = 10
BOARDS = {"points": "Points 1+", "goals": "Goals anytime", "assists": "Assists 1+",
          "ppp": "PP points 1+", "sog": "Shots on goal"}
LOW_TOI = (12.0, 16.9)          # Devin's gate: TOI L10 strictly inside this band
LOW_TOI_PTS60, LOW_TOI_AST60 = 3.0, 2.0
LOW_TOI_MAX = 8
WEAK_SV = 0.888                 # his trailing-script goalie threshold

NOTE = ("Scores = PropLine percentile rank 0-100 within tonight's slate, not probabilities. "
        "proj = PropLine projected goals (GF/xGF vs GA/xGA, goalie, home ice; calibrated "
        "to league scoring). Market = US consensus game lines; no player prop lines.")


def _r(v, nd=2):
    """Round, and turn NaN / None into None so the JSON stays clean."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v if isinstance(v, str) and v else None
    if math.isnan(f):
        return None
    return int(round(f)) if nd == 0 else round(f, nd)


def _et(iso) -> str | None:
    if not isinstance(iso, str):
        return None
    t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(ET)
    return t.strftime("%I:%M%p").lstrip("0").lower()


def _status(state) -> str:
    return {"FUT": "scheduled", "PRE": "scheduled", "LIVE": "LIVE", "CRIT": "LIVE",
            "OFF": "final", "FINAL": "final"}.get(str(state), "scheduled")


def _goalie(s, sv) -> dict:
    if s is None:
        return {}
    if s.get("manual"):
        st = "confirmed (manual)"
    elif s.get("status") == "confirmed":
        st = "confirmed"
    else:
        st = f"projected ({s.get('confidence')})"
    v = _r(sv.get(s.get("goalie_id")), 3)
    return {"name": s.get("goalie_name"), "sv": v, "status": st,
            **({"weak_sv": True} if v is not None and v <= WEAK_SV else {})}


def build(day: str, schedule: pd.DataFrame, starters: pd.DataFrame, quality: pd.DataFrame,
          avail: pd.DataFrame, player_scores: pd.DataFrame, total_goals: pd.DataFrame,
          boards: dict, record: dict | None, run_kind: str) -> dict:
    sv = dict(zip(quality["goalie_id"], quality["sv_pct"])) if quality is not None else {}
    st = {r["team"]: r for r in starters.to_dict("records")} if not starters.empty else {}
    tg = boards.get("team_goals", pd.DataFrame())
    tg = {r["team"]: r for r in tg.to_dict("records")} if not tg.empty else {}
    ml = boards.get("moneyline", pd.DataFrame())
    ml = {int(r["game_id"]): r for r in ml.to_dict("records")} if not ml.empty else {}
    pl = boards.get("puck_line", pd.DataFrame())
    pl = {int(r["game_id"]): r for r in pl.to_dict("records")} if not pl.empty else {}
    tot = ({int(r["game_id"]): r for r in total_goals.to_dict("records")}
           if total_goals is not None and not total_goals.empty else {})

    out_by_team: dict[str, list[str]] = {}
    if avail is not None and not avail.empty:
        o = avail[(avail["status"] == "out") & (avail["regular"] | avail["status_source"].eq("manual"))]
        for _, r in o.iterrows():
            tag = " (manual)" if r["status_source"] == "manual" else ""
            out_by_team.setdefault(r["team"], []).append(f"{r['player_name']}{tag}")

    games, teams = [], []
    for _, g in schedule.sort_values("game_time_utc").iterrows():
        gid, a, h = int(g["game_id"]), g["away_team"], g["home_team"]
        teams += [a, h]
        t, m, p = tot.get(gid, {}), ml.get(gid, {}), pl.get(gid, {})
        side = {}
        for team in (a, h):
            x = tg.get(team, {})
            s = st.get(team)
            side[team] = {k: v for k, v in {
                "proj": _r(x.get("proj_goals")), "tag": x.get("tt_tag") or None,
                "mkt_tt": _r(x.get("market_team_total"), 1),
                "ml": _r(m.get("pick_ml") if m.get("pick") == team else m.get("dog_ml"), 0),
                "b2b": bool(s.get("back_to_back")) if s is not None else None,
                "goalie": _goalie(s, sv),
                "out": out_by_team.get(team) or None,
            }.items() if v not in (None, False)}
        games.append({k: v for k, v in {
            "id": gid, "time_et": _et(g.get("game_time_utc")), "away": a, "home": h,
            "status": _status(g.get("state")),
            "proj_total": _r(t.get("proj_total")), "mkt_total": _r(t.get("market_total"), 1),
            "over": _r(t.get("over_price"), 0), "under": _r(t.get("under_price"), 0),
            "lean": (t.get("model_lean") or "").replace("MODEL LEAN: ", "") or None,
            "stronger": m.get("pick"), "edge": m.get("edge"), "mkt_fav": m.get("market_fav"),
            "pl": (f"{p.get('pick')} {_r(p.get('pick_pl_point'), 1):+g} @ {int(p['pick_pl_price']):+d}"
                   if p.get("pick") is not None and _r(p.get("pick_pl_point"), 1) is not None
                   and _r(p.get("pick_pl_price"), 0) is not None else None),
            "teams": side,
        }.items() if v is not None})

    # --- players: one shared row each, boards reference ids ---------------------------
    ps = player_scores if player_scores is not None else pd.DataFrame()
    base = ps[ps["prop"] == "points"].set_index("player_id") if not ps.empty else pd.DataFrame()
    board_ids, wanted = {}, set()
    for prop in BOARDS:
        b = ps[ps["prop"] == prop].sort_values("rank").head(TOP_PER_BOARD) if not ps.empty else ps
        board_ids[prop] = [[int(r["player_id"]), int(r["rank"]), _r(r["score"], 1)]
                           for _, r in b.iterrows()]
        wanted |= {int(x) for x in b["player_id"]} if not b.empty else set()

    low = pd.DataFrame()
    if not base.empty:
        cand = base[(base["toi_recent"] > LOW_TOI[0]) & (base["toi_recent"] < LOW_TOI[1]) &
                    ((base["points60"] > LOW_TOI_PTS60) | (base["assists60"] > LOW_TOI_AST60))]
        low = cand.sort_values("points60", ascending=False).head(LOW_TOI_MAX)
        wanted |= set(int(i) for i in low.index)

    sog = ps[ps["prop"] == "sog"].set_index("player_id") if not ps.empty else pd.DataFrame()
    players = {}
    for pid in sorted(wanted):
        if pid not in base.index:
            continue
        r = base.loc[pid]
        s = sog.loc[pid] if pid in sog.index else {}
        flags = [x for x in (r.get("check_note"), r.get("override_note"),
                             r.get("goalie_check")) if isinstance(x, str) and x]
        players[str(pid)] = {k: v for k, v in {
            "name": r["player_name"], "team": r["team"], "opp": r["opponent"],
            "pos": r.get("position"),
            "toi": [_r(r.get("toi_l5"), 1), _r(r.get("toi_recent"), 1)],        # L5, L10
            "pp_toi": _r(r.get("pp_toi_recent"), 1),
            "pts60": _r(r.get("points60")), "ast60": _r(r.get("assists60")),
            # points per game: L3, L5, L10, season (this season, or last season early on)
            "pts_pg": [_r(r.get("points_pg_l3")), _r(r.get("points_pg_l5")),
                       _r(r.get("points_pg_recent")), _r(r.get("pts_season"))],
            "sog_pg": [_r(s.get("sog_pg_l3"), 1), _r(s.get("sog_pg_l5"), 1),
                       _r(s.get("sog_pg_recent"), 1), _r(s.get("sog_season"), 1)],
            "sog_line": ((f"safe {s.get('safe_sog')}"
                          + (f", aggr {s.get('aggressive_sog')}"
                             if isinstance(s.get("aggressive_sog"), str) else ""))
                         if isinstance(s.get("safe_sog"), str) else None),
            "flags": flags or None,
        }.items() if v is not None}

    return {
        "slate_date": day,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "run": run_kind,
        "note": NOTE,
        "teams_today": sorted(set(teams)),
        "games": games,
        "players": players,
        "boards": board_ids,
        "low_toi": [int(i) for i in low.index] if not low.empty else [],
        "low_toi_rule": "TOI L10 12.0-16.9 min and (Pts/60 > 3.0 or Ast/60 > 2.0)",
        "track_record": record or {},
    }


def track_record(read_all) -> dict:
    """Top-10 hit rate vs base rate per graded board, last 30 days and season."""
    try:
        rows = pd.DataFrame(read_all("pick_results", {"select": "slate_date,board,rank,result,base_p"}))
    except Exception:  # noqa: BLE001 - before the Results tables exist
        return {}
    if rows.empty:
        return {}
    rows = rows[rows["result"] != "void"]
    rows["base_p"] = pd.to_numeric(rows["base_p"], errors="coerce")
    cut = (datetime.now(ET).date() - timedelta(days=30)).isoformat()
    out = {}
    for board, g in rows.groupby("board"):
        top = g[g["rank"] <= 10]
        if top.empty:
            continue
        r30 = top[top["slate_date"] >= cut]
        out[board] = {"top10_30d": [int((r30["result"] == "hit").sum()), len(r30),
                                    _r(r30["base_p"].mean())],
                      "top10_season": [int((top["result"] == "hit").sum()), len(top),
                                       _r(top["base_p"].mean())]}
    return {"since": str(rows["slate_date"].min()),
            "format": "[hits, graded, base rate]", "boards": out}
