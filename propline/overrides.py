"""Manual overrides (order 4, Nov 2026): Devin's corrections for today's slate.

Set in the admin panel (web/admin.html -> netlify/functions/override.mjs), stored in
manual_overrides (db/overrides.sql), applied here at collect time:

  - kind "goalie", value "confirmed": that goalie starts. Counts as CONFIRMED everywhere
    (clears the Goalie Check warning and the lean-goalie suppression of the model-lean
    badge), labelled "Goalie confirmed manually".
  - kind "status", value "out" / "in": the skater is out (minutes redistributed exactly
    like a game-roster OUT) or in (overrides a recent-games absence and the
    "left last game early" flag), labelled "Marked out / in manually".

Priority: official final game roster > manual override > projection. So a manual goalie
who is not on tonight's posted game roster, or a manual IN for a skater the roster
leaves off, is ignored (and the reason says so); the boxscore's own starter flag beats
a manual goalie once the game is on.

An override set at or after its game's puck drop is ignored (the database trigger also
refuses it), so the Results freeze only ever sees overrides made before the game.
"""

from __future__ import annotations

import pandas as pd

from .db import read

COLS = ["id", "slate_date", "kind", "team", "player_id", "player_name", "value",
        "game_id", "game_start", "set_at"]
GOALIE_NOTE = "Goalie confirmed manually"
OUT_NOTE = "Marked out manually"
IN_NOTE = "Marked in manually"


def load(day: str) -> pd.DataFrame:
    """Active overrides for the slate, made before their game's start. Empty (with
    columns) when there are none or the table does not exist yet."""
    try:
        rows = read("manual_overrides", {"select": ",".join(COLS),
                                         "slate_date": f"eq.{day}",
                                         "removed_at": "is.null", "order": "set_at.asc",
                                         "limit": "1000"})
    except Exception as exc:  # noqa: BLE001 - table missing until db/overrides.sql is run
        print(f"  WARN  manual overrides unavailable: {exc}")
        return pd.DataFrame(columns=COLS)
    df = pd.DataFrame(rows, columns=COLS)
    if df.empty:
        return df
    ok = pd.to_datetime(df["set_at"], utc=True) < pd.to_datetime(df["game_start"], utc=True)
    late = df[~ok]
    for _, r in late.iterrows():
        print(f"  WARN  override ignored, set after puck drop: {r['kind']} {r['team']} "
              f"{r['player_name']}")
    df = df[ok].copy()
    df["player_id"] = df["player_id"].astype("int64")
    return df


def goalie_overrides(ov: pd.DataFrame) -> dict[str, int]:
    """team -> goalie_id confirmed manually."""
    if ov is None or ov.empty:
        return {}
    g = ov[(ov["kind"] == "goalie") & (ov["value"] == "confirmed")]
    return dict(zip(g["team"], g["player_id"].astype(int)))


def status_overrides(ov: pd.DataFrame) -> dict[int, str]:
    """player_id -> "out" | "in"."""
    if ov is None or ov.empty:
        return {}
    s = ov[ov["kind"] == "status"]
    return dict(zip(s["player_id"].astype(int), s["value"]))


def summary(ov: pd.DataFrame) -> str:
    if ov is None or ov.empty:
        return "none"
    g = int((ov["kind"] == "goalie").sum())
    o = int(((ov["kind"] == "status") & (ov["value"] == "out")).sum())
    i = int(((ov["kind"] == "status") & (ov["value"] == "in")).sum())
    return f"{len(ov)} ({g} goalie, {o} out, {i} in)"
