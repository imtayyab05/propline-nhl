"""L5 / L3 columns beside every L10 column (client ask, 16 Oct 2026). Display only.

One rule, used by the Excel sheets and the published details alike, so a new L10
column automatically gets its L5 / L3 neighbours. The dashboard has the same rule in
web/index.html (`withRecent`). The numbers themselves are computed in
scoring.player_rates, teams.team_table, shots.team_xg_table, games.team_context and
trends.player_trends - and none of them is read by a weight.
"""

from __future__ import annotations

# Game and team boards show season (blended) numbers rather than L10 ones; for the stats
# the client named (goals, SOG and PP for and against) the L5 / L3 sit beside those.
SEASON_TO_RECENT = {
    "opp_ga": "opp_l{w}_ga", "pp_pct": "l{w}_pp_pct", "ppg": "l{w}_ppg",
    "away_sf": "away_l{w}_sf", "away_sa": "away_l{w}_sa",
    "home_sf": "home_l{w}_sf", "home_sa": "home_l{w}_sa",
    "away_pp_pct": "away_l{w}_pp_pct", "home_pp_pct": "home_l{w}_pp_pct",
    "away_gf_pg": "away_l{w}_gf_pg", "away_ga_pg": "away_l{w}_ga_pg",
    "home_gf_pg": "home_l{w}_gf_pg", "home_ga_pg": "home_l{w}_ga_pg",
}


def recent_keys(key: str) -> list[str]:
    """The L5 and L3 keys that belong beside `key`, or [] if it has none."""
    out = []
    for w in (5, 3):
        if key.endswith("_recent"):
            out.append(f"{key[:-len('_recent')]}_l{w}")
        elif key.endswith("_l10"):
            out.append(f"{key[:-len('_l10')]}_l{w}")
        elif key.startswith("l10_"):
            out.append(f"l{w}_{key[4:]}")
        elif "_l10_" in key:
            out.append(key.replace("_l10_", f"_l{w}_"))
        elif key in SEASON_TO_RECENT:
            out.append(SEASON_TO_RECENT[key].format(w=w))
    return out


def _label(label: str, w: int) -> str:
    if "L10" in label:
        return label.replace("L10", f"L{w}")
    return f"{label} L{w}"


def with_recent(spec: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Excel column spec with L5 / L3 inserted right after each L10 (or season) column."""
    out = []
    for src, label in spec:
        out.append((src, label))
        for w, key in zip((5, 3), recent_keys(src)):
            out.append((key, _label(label, w)))
    return out


def expand_keys(keys: list[str]) -> list[str]:
    """Detail-key list with the L5 / L3 keys added (for the published JSON)."""
    out = []
    for k in keys:
        out.append(k)
        out.extend(recent_keys(k))
    return out
