"""Client-facing Excel output — one tab per prop, plus team stats and goalies.

Written for a human reading it before puck drop: plain column names, best plays at the
top, and the status of everything that is still a projection (lineup, goalie) visible
on every row so a projection is never mistaken for a confirmation.

Each prop gets its OWN middle columns — the numbers that actually drive that board.
The internal exp_* volume signals are deliberately absent: see propline/scoring.py.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

HEAD = [
    ("rank", "#"),
    ("player_name", "Player"),
    ("team", "Team"),
    ("position", "Pos"),
    ("opponent", "Opp"),
    ("score", "Score"),
    ("lineup_status", "Lineup"),
    ("recent_games", "Recent G"),
]
TAIL = [
    ("toi_recent", "TOI (L10)"),
    ("toi_bump", "+TOI (absences)"),
    ("bump_from", "Minutes From"),
    ("opp_missing", "Opp Missing"),
    ("rationale", "Why"),
]

PROP_MIDDLE = {
    "sog": [
        ("sog_pg_recent", "SOG/G (L10)"),
        ("shots60", "SOG/60 (season)"),
        ("opp_sa_pos", "Opp SOG Allowed to Pos/G"),
        ("hd_pg", "Dangerous Shots/G"),
        ("safe_sog", "Safe SOG Line (L10)"),
        ("aggressive_sog", "Aggressive SOG Line (L10)"),
    ],
    "goals": [
        ("goals_recent", "Goals (L10)"),
        ("goal1_l10", "Anytime Goal (L10)"),
        ("goals60", "Goals/60 (season)"),
        ("sh_pct", "Shooting % (adj)"),
        ("ixg60", "xG/60"),
        ("hd_pg", "Dangerous Shots/G"),
        ("sog_pg_recent", "SOG/G (L10)"),
        ("pp_toi_recent", "PP TOI (L10)"),
        ("opp_goalie", "Opp Goalie"),
        ("opp_goalie_sv", "Opp Goalie SV% (adj)"),
        ("opp_goalie_status", "Goalie Status"),
        ("goalie_check", "Goalie Check"),
    ],
    "assists": [
        ("assists_recent", "Assists (L10)"),
        ("ast1_l10", "1+ Assist (L10)"),
        ("assists60", "Assists/60 (season)"),
        ("pp_toi_recent", "PP TOI (L10)"),
        ("team_gf", "Team GF/G"),
        ("opp_ga", "Opp GA/G"),
        ("goalie_check", "Goalie Check"),
    ],
    "points": [
        ("points_pg_recent", "Pts/G (L10)"),
        ("pts1_l10", "1+ Pts (L10)"),
        ("pts2_l10", "2+ Pts (L10)"),
        ("points60", "Pts/60 (season)"),
        ("ixg60", "xG/60"),
        ("pp_toi_recent", "PP TOI (L10)"),
        ("opp_ga", "Opp GA/G"),
        ("opp_goalie", "Opp Goalie"),
        ("opp_goalie_sv", "Opp Goalie SV% (adj)"),
        ("opp_goalie_status", "Goalie Status"),
        ("goalie_check", "Goalie Check"),
    ],
    "ppp": [
        ("ppp_recent", "PPP (L10)"),
        ("ppp_pg", "PPP/G (season)"),
        ("pp_toi_recent", "PP TOI (L10)"),
        ("pp_toi_bump", "+PP TOI (absences)"),
        ("opp_pen_taken", "Opp Times SH/G"),
        ("opp_pk_pct", "Opp PK%"),
        ("goalie_check", "Goalie Check"),
    ],
}

TREND_MIDDLE = {
    "sog_lines": [
        ("safe_sog", "Safe Line (L10)"), ("aggressive_sog", "Aggressive Line (L10)"),
        ("sog1_l10", "1+ L10"), ("sog2_l10", "2+ L10"), ("sog3_l10", "3+ L10"),
        ("sog4_l10", "4+ L10"),
        ("sog1_l20", "1+ L20"), ("sog2_l20", "2+ L20"), ("sog3_l20", "3+ L20"),
        ("sog4_l20", "4+ L20"),
        ("sog2_season", "2+ Season"), ("sog3_season", "3+ Season"),
        ("season_basis", "Season"), ("opp_sa_pos", "Opp SOG Allowed to Pos/G"),
        ("toi_bump", "+TOI (absences)"),
    ],
    "multi_point": [
        ("pts1_l10", "1+ Pts L10"), ("pts2_l10", "2+ Pts L10"), ("pts2_l20", "2+ Pts L20"),
        ("pts2_season", "2+ Pts Season"), ("season_basis", "Season"),
        ("points_pg_recent", "Pts/G L10"), ("pp_toi_recent", "PP TOI (L10)"),
        ("opp_ga", "Opp GA/G"), ("goalie_check", "Goalie Check"),
    ],
    "multi_goal": [
        ("goal1_l10", "Anytime L10"), ("goal1_l20", "Anytime L20"),
        ("goal2_l20", "2+ Goals L20"), ("goal2_season", "2+ Goals Season"),
        ("goal2_last_season", "2+ Goals Last Season"), ("season_basis", "Season"),
        ("ixg60", "xG/60"), ("hd_pg", "Dangerous Shots/G"),
        ("goalie_check", "Goalie Check"),
    ],
    "streaks": [
        ("form", "Form"), ("point_streak", "Point Streak"), ("goal_streak", "Goal Streak"),
        ("sog3_streak", "3+ SOG Streak"), ("pointless", "Games Without a Point"),
        ("goalless", "Games Without a Goal"),
        ("pts_l5", "Pts/G L5"), ("pts_season", "Pts/G Season"),
        ("sog_l5", "SOG/G L5"), ("sog_season", "SOG/G Season"),
        ("goals_l5", "Goals/G L5"), ("goals_season", "Goals/G Season"),
        ("season_basis", "Season"),
    ],
    "h2h": [
        ("h2h_summary", "Vs Tonight's Opponent"), ("h2h_gp", "GP"), ("h2h_goals", "G"),
        ("h2h_assists", "A"), ("h2h_points", "P"), ("h2h_pts1", "1+ Pts"),
        ("h2h_sog_pg", "SOG/G"), ("h2h_seasons", "Seasons"), ("dvp_note", "Opp DvP"),
    ],
}
PROP_MIDDLE.update(TREND_MIDDLE)

SHEET_TITLES = {"sog": "Shots on Goal", "points": "Points", "goals": "Goals",
                "assists": "Assists", "ppp": "PP Points",
                "sog_lines": "SOG Lines", "multi_point": "2+ Points",
                "multi_goal": "Multi-Goal", "streaks": "Streaks", "h2h": "Head-to-Head"}

# DvP: what each team allows to centres, wingers and defence (all at that position
# combined, per game), with its rank among 32 teams (1 = allows the most).
DVP_COLS = [("team", "Team")] + [
    (f"dvp_{pg}_{s}_{w}{r}", f"{name} {lab} {wl}{' Rank' if r else ''}")
    for pg, name in (("C", "To C"), ("W", "To W"), ("D", "To D"))
    for s, lab in (("sog", "SOG/G"), ("pts", "Pts/G"), ("g", "G/G"))
    for w, wl in (("season", "Season"), ("l10", "L10"), ("last", "Last Season"))
    for r in ("", "_rank")
] + [("dvp_gp_season", "GP This Season")]

TOTAL_GOALS_COLS = [
    ("rank", "#"), ("matchup", "Matchup"), ("score", "Score"),
    ("away_gf_pg", "Away GF/G"), ("away_ga_pg", "Away GA/G"),
    ("home_gf_pg", "Home GF/G"), ("home_ga_pg", "Home GA/G"),
    ("away_goalie", "Away Goalie"), ("away_goalie_sv", "Away SV% (adj)"),
    ("home_goalie", "Home Goalie"), ("home_goalie_sv", "Home SV% (adj)"),
    ("goalies_status", "Goalies (away / home)"),
    ("goalie_check", "Goalie Check"),
    ("combined_pp_threat", "PP Threat Index"),
    ("missing_names", "Regulars Out"),
    ("combined_xg", "Combined xG/G"),
    ("market_total", "Market Total"), ("over_price", "Over"), ("under_price", "Under"),
    ("rationale", "Why"),
]

# Phase 2 boards. Market columns are US-book consensus lines (propline/odds.py); a blank
# means no line yet, or a market no book offers (SOG and PP-goal totals never have one).
BOARD_SHEETS = {
    "moneyline": ("Moneyline", [
        ("rank", "#"), ("matchup", "Matchup"), ("pick", "Stronger Side"),
        ("edge", "Edge"), ("score", "Score"), ("pick_ml", "Its ML"),
        ("dog_ml", "Other ML"), ("market_fav", "Market Favourite"),
        ("market_agrees", "Market Agrees"), ("pick_goalie", "Its Goalie"),
        ("dog_goalie", "Other Goalie"), ("goalie_check", "Goalie Check"),
        ("pick_b2b", "Its B2B"), ("dog_b2b", "Other B2B"),
        ("rationale", "Why")]),
    "puck_line": ("Puck Line", [
        ("rank", "#"), ("subject", "Play"), ("matchup", "Matchup"), ("edge", "Edge"),
        ("score", "Score"), ("margin_fuel", "Its GF + Their GA"),
        ("dog_goalie", "Goalie Faced"), ("goalie_check", "Goalie Check"),
        ("pick_pl_point", "Market PL"),
        ("pick_pl_price", "PL Price"), ("market_fav", "Market Favourite"),
        ("rationale", "Why")]),
    "team_goals": ("Team Goals", [
        ("rank", "#"), ("team", "Team"), ("opponent", "Opp"), ("score", "Score"),
        ("gf", "GF/G"), ("xgf", "xGF/G"), ("opp_ga", "Opp GA/G"),
        ("opp_xga", "Opp xGA/G"), ("opp_goalie", "Goalie Faced"),
        ("opp_goalie_sv", "Its SV% (adj)"), ("goalie_check", "Goalie Check"),
        ("l10_gf", "GF/G L10"),
        ("market_team_total", "Market Team Total"), ("tt_over_price", "Over"),
        ("tt_under_price", "Under"), ("missing_names", "Regulars Out"),
        ("rationale", "Why")]),
    "team_sog": ("Team SOG", [
        ("rank", "#"), ("team", "Team"), ("opponent", "Opp"), ("score", "Score"),
        ("sf", "SOG/G"), ("opp_sa", "Opp SOG Agst/G"), ("l10_sf", "SOG/G L10"),
        ("opp_l10_sa", "Opp SOG Agst/G L10"), ("missing_names", "Regulars Out"),
        ("rationale", "Why")]),
    "game_sog": ("Game SOG", [
        ("rank", "#"), ("matchup", "Matchup"), ("score", "Score"),
        ("away_sf", "Away SOG/G"), ("away_sa", "Away SOG Agst/G"),
        ("home_sf", "Home SOG/G"), ("home_sa", "Home SOG Agst/G"),
        ("rationale", "Why")]),
    "team_ppg": ("Team PPG", [
        ("rank", "#"), ("team", "Team"), ("opponent", "Opp"), ("score", "Score"),
        ("pp_pct", "PP%"), ("ppg", "PPG/G"), ("opp_pen_taken", "Opp Times SH/G"),
        ("opp_pk_pct", "Opp PK%"), ("opp_ppga", "Opp PPG Agst/G"),
        ("goalie_check", "Goalie Check"), ("rationale", "Why")]),
    "game_ppg": ("Game PPG", [
        ("rank", "#"), ("matchup", "Matchup"), ("score", "Score"),
        ("away_pp_pct", "Away PP%"), ("home_pp_pct", "Home PP%"),
        ("home_opp_pen_taken", "Away Times SH/G"),
        ("away_opp_pen_taken", "Home Times SH/G"), ("ppg_sum", "Combined PPG/G"),
        ("goalie_check", "Goalie Check"),
        ("rationale", "Why")]),
}

TEAM_COLS = [
    ("power_rank", "Power #"), ("team", "Team"), ("power_score", "Power Score"),
    ("gp", "GP"), ("record", "Record"), ("points", "Pts"), ("points_pct", "Pts%"),
    ("gf_pg", "GF/G"), ("l10_gf_pg", "GF/G L10"),
    ("ga_pg", "GA/G"), ("l10_ga_pg", "GA/G L10"),
    ("xgf_pg", "xGF/G"), ("l10_xgf_pg", "xGF/G L10"),
    ("xga_pg", "xGA/G"), ("l10_xga_pg", "xGA/G L10"),
    ("sf_pg", "SOG/G"), ("l10_sf_pg", "SOG/G L10"),
    ("sa_pg", "SOG Agst/G"), ("l10_sa_pg", "SOG Agst/G L10"),
    ("ppg_pg", "PPG/G"), ("l10_ppg_pg", "PPG/G L10"),
    ("ppga_pg", "PPG Agst/G"), ("l10_ppga_pg", "PPG Agst/G L10"),
    ("pp_pct", "PP%"), ("l10_pp_pct", "PP% L10"),
    ("pk_pct", "PK%"), ("l10_pk_pct", "PK% L10"),
    ("pen_taken_pg", "Times SH/G"), ("l10_pen_taken_pg", "Times SH/G L10"),
    ("l10_record", "L10 Record"),
    ("current_weight", "This-Season Weight"),
]

GOALIE_COLS = [
    ("team", "Team"), ("goalie_name", "Goalie"), ("status", "Status"),
    ("confidence", "Confidence"), ("reason", "Why"), ("sv_pct", "SV% (adj)"),
    ("starts_recent", "Recent Starts"), ("team_games_recent", "Team Games"),
    ("last_start", "Last Start"), ("back_to_back", "Back-to-Back"),
    ("backup_name", "Other Goalie"),
]

OUT_COLS = [
    ("team", "Team"), ("player_name", "Player"), ("position", "Pos"),
    ("base_toi", "Usual TOI"), ("base_pp_toi", "Usual PP TOI"),
    ("games_missed", "Games Missed"), ("reason", "Why"),
    ("status_source", "Source"),
]


def columns_for(prop: str):
    # trend tabs are track records: no ice-time tail and no Why column
    if prop in TREND_MIDDLE:
        return HEAD + PROP_MIDDLE[prop]
    return HEAD + PROP_MIDDLE.get(prop, []) + TAIL


def _shape(df: pd.DataFrame, spec, top_n=None) -> pd.DataFrame:
    cols = [(src, label) for src, label in spec if src in df.columns]
    out = df[[src for src, _ in cols]].copy()
    out.columns = [label for _, label in cols]
    return out.head(top_n) if top_n else out


def build_picks_workbook(player_scores, total_goals, team_tbl, starters, outs,
                         out_path, run_meta: dict, top_n: int | None = None,
                         game_boards: dict | None = None) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(out_path, engine="openpyxl") as xl:
        pd.DataFrame(list(run_meta.items()), columns=["Field", "Value"]).to_excel(
            xl, sheet_name="Summary", index=False)

        for prop, title in SHEET_TITLES.items():
            if player_scores is None or player_scores.empty:
                continue
            src = player_scores[player_scores["prop"] == prop]
            if src.empty:
                continue
            _shape(src.sort_values("rank"), columns_for(prop), top_n).to_excel(
                xl, sheet_name=title, index=False)

        # slate-wide tabs are never truncated
        boards = [(total_goals, TOTAL_GOALS_COLS, "Total Goals")]
        for prop, (title, spec) in BOARD_SHEETS.items():
            boards.append(((game_boards or {}).get(prop), spec, title))
        for frame, spec, title in (*boards,
                                   (team_tbl, TEAM_COLS, "Team Stats"),
                                   (team_tbl, DVP_COLS, "DvP"),
                                   (starters, GOALIE_COLS, "Goalies"),
                                   (outs, OUT_COLS, "Out Tonight")):
            if frame is not None and not frame.empty:
                _shape(frame, spec).to_excel(xl, sheet_name=title, index=False)

        for ws in xl.book.worksheets:
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 7), 46)

    return out_path
