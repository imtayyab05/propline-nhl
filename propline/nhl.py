"""Official NHL feed — the only data source for Phase 1.

Two hosts, both public and keyless:
  api-web.nhle.com     schedule, current rosters, gamecenter (boxscore / play-by-play)
  api.nhle.com/stats   the stats REST API — game-by-game rows for every skater, goalie
                       and team in ONE request per report, which is what makes this
                       cheap: no per-game boxscore crawl is needed for form windows.

Traps found while probing (Oct 2026), kept here because they WILL bite again:
- The stats API caps every response at 10,000 rows and reports `total` as 10,000 when
  it is capped, so a big pull looks complete when it is not. Skater game logs are
  pulled in date chunks and any chunk that comes back at the cap is a hard error.
- Team rows carry teamId and full name but NO abbreviation; skater and goalie rows
  carry the abbreviation but no id. TEAMS below is the bridge.
- `starter` on a goalie exists only in the boxscore AFTER puck drop. Pre-game there is
  no starter field anywhere in the feed — see propline/goalies.py.
- play-by-play `rosterSpots` (the dressed game roster) is empty for future games.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

WEB = "https://api-web.nhle.com/v1"
STATS = "https://api.nhle.com/stats/rest/en"
TIMEOUT = 60
RETRIES = 3
ROW_CAP = 10_000

# gameType in the schedule feed / gameTypeId in the stats API
REGULAR, PLAYOFFS = 2, 3

_session = requests.Session()
_session.headers["User-Agent"] = "PropLine-NHL/1.0"


class FeedError(RuntimeError):
    pass


def _get(url: str, params: dict | None = None) -> dict:
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = _session.get(url, params=params, timeout=TIMEOUT)
            if r.ok:
                return r.json()
            if 400 <= r.status_code < 500 and r.status_code != 429:
                raise FeedError(f"{url}: {r.status_code} {r.text[:200]}")
            last = FeedError(f"{url}: {r.status_code}")
        except requests.RequestException as exc:
            last = FeedError(f"{url}: {exc}")
        if attempt < RETRIES:
            time.sleep(2 ** attempt)
    raise last


# --- seasons ------------------------------------------------------------------

def season_of(day: date) -> int:
    """20262027 for any date from July 2026 to June 2027."""
    y = day.year if day.month >= 7 else day.year - 1
    return y * 10000 + y + 1


def previous_season(season: int) -> int:
    y = season // 10000 - 1
    return y * 10000 + y + 1


# --- teams --------------------------------------------------------------------

def get_teams() -> pd.DataFrame:
    """team_id, abbrev, team_name for every franchise id the stats API knows."""
    rows = _get(f"{STATS}/team")["data"]
    return pd.DataFrame([{"team_id": r["id"], "abbrev": r["triCode"],
                          "team_name": r["fullName"]} for r in rows])


# --- schedule -----------------------------------------------------------------

SCHEDULE_COLS = ["game_id", "game_date", "game_time_utc", "game_type", "state",
                 "venue", "home_team", "home_team_id", "away_team", "away_team_id"]


def get_schedule(day: str) -> pd.DataFrame:
    """Regular-season and playoff games on one date. Preseason is dropped.

    Always returns the columns, even on an off-day — an empty frame without columns
    crashed MLB's matchup step with KeyError on the first off-day of the season.
    """
    j = _get(f"{WEB}/schedule/{day}")
    rows = []
    for d in j.get("gameWeek", []):
        if d["date"] != day:
            continue
        for g in d["games"]:
            if g.get("gameType") not in (REGULAR, PLAYOFFS):
                continue
            if g.get("gameScheduleState") not in (None, "OK"):
                continue                      # postponed / cancelled
            rows.append({
                "game_id": g["id"], "game_date": day,
                "game_time_utc": g.get("startTimeUTC"),
                "game_type": g["gameType"], "state": g.get("gameState"),
                "venue": (g.get("venue") or {}).get("default"),
                "home_team": g["homeTeam"]["abbrev"], "home_team_id": g["homeTeam"]["id"],
                "away_team": g["awayTeam"]["abbrev"], "away_team_id": g["awayTeam"]["id"],
            })
    return pd.DataFrame(rows, columns=SCHEDULE_COLS)


def team_game_dates(team_logs: pd.DataFrame) -> dict[str, list[str]]:
    """Every date each team has played, oldest first — for rest days and back-to-backs."""
    if team_logs.empty:
        return {}
    return {t: sorted(g["game_date"].astype(str).unique())
            for t, g in team_logs.groupby("team")}


# --- rosters ------------------------------------------------------------------

ROSTER_COLS = ["team", "player_id", "player_name", "position", "pos_group", "shoots"]


def get_roster(abbrev: str) -> pd.DataFrame:
    """Current roster for one club. The team a player is on TODAY — the game logs only
    know which team he played for at the time, which is wrong for anyone traded or
    signed over the summer."""
    j = _get(f"{WEB}/roster/{abbrev}/current")
    rows = []
    for key, grp in (("forwards", "F"), ("defensemen", "D"), ("goalies", "G")):
        for p in j.get(key, []):
            rows.append({
                "team": abbrev, "player_id": p["id"],
                "player_name": f"{p['firstName']['default']} {p['lastName']['default']}",
                "position": p.get("positionCode"), "pos_group": grp,
                "shoots": p.get("shootsCatches"),
            })
    return pd.DataFrame(rows, columns=ROSTER_COLS)


def get_rosters(abbrevs) -> pd.DataFrame:
    frames = [get_roster(a) for a in sorted(set(abbrevs))]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=ROSTER_COLS)


# --- game-day rosters and confirmed starters ------------------------------------

DRESSED_COLS = ["game_id", "team_id", "player_id", "position", "source"]
# States in which no goalie has played yet. Every OTHER state is checked for a starter.
# The first version listed the started states instead (LIVE, CRIT, OFF, FINAL) and
# missed OVER - a game just ended, not yet official - so the late run, which lands as
# the 7pm games finish, confirmed 0 of 18 goalies on 6 Oct 2026. Listing the few
# not-started states means a state the league adds later is checked, not skipped.
NOT_STARTED = {"FUT", "PRE"}
STARTER_COLS = ["game_id", "team_id", "goalie_id"]


def get_game_day(game_ids) -> tuple[pd.DataFrame, pd.DataFrame]:
    """What the league itself has published for today's games so far.

    dressed:  the official game roster (play-by-play rosterSpots). Empty until the
              clubs submit lines; when present it is the authoritative scratch list.
    starters: goalies flagged `starter` in the boxscore — only exists once the game is
              under way, so it confirms after the fact and never before.
    Both come back empty (with columns) when nothing is published yet; that is the
    normal state for most of the day, not an error.
    """
    dressed, starters, states = [], [], {}
    for gid in game_ids:
        try:
            pbp = _get(f"{WEB}/gamecenter/{gid}/play-by-play")
        except FeedError as exc:
            print(f"  WARN  game roster for {gid} unavailable: {exc}")
            continue
        for s in pbp.get("rosterSpots", []) or []:
            dressed.append({"game_id": gid, "team_id": s["teamId"],
                            "player_id": s["playerId"],
                            "position": s.get("positionCode"), "source": "game_roster"})
        state = pbp.get("gameState") or "?"
        states[state] = states.get(state, 0) + 1
        if state not in NOT_STARTED:
            try:
                box = _get(f"{WEB}/gamecenter/{gid}/boxscore")
            except FeedError as exc:
                print(f"  WARN  boxscore for {gid} unavailable: {exc}")
                continue
            for side in ("homeTeam", "awayTeam"):
                tid = box[side]["id"]
                goalies = box.get("playerByGameStats", {}).get(side, {}).get("goalies", [])
                flagged = [gk for gk in goalies if gk.get("starter")]
                if not flagged:
                    # No starter flag yet (early in a live game): the one goalie who has
                    # logged ice time started. Two with time = a pull; leave it unset
                    # rather than guess which one started.
                    played = [gk for gk in goalies if _secs(gk.get("toi")) > 0]
                    flagged = played if len(played) == 1 else []
                for gk in flagged[:1]:
                    starters.append({"game_id": gid, "team_id": tid,
                                     "goalie_id": gk["playerId"]})
    out = (pd.DataFrame(dressed, columns=DRESSED_COLS),
           pd.DataFrame(starters, columns=STARTER_COLS))
    out[1].attrs["states"] = states       # for the run log: what the feed said
    return out


def _secs(toi) -> int:
    """'12:34' -> 754; anything unreadable -> 0."""
    try:
        m, s = str(toi).split(":")
        return int(m) * 60 + int(s)
    except (ValueError, AttributeError):
        return 0


# --- stats REST API -------------------------------------------------------------

def _stats(report: str, cayenne: str, game_level: bool = True) -> list[dict]:
    """One stats-API report, refusing to return a silently truncated result."""
    params = {"isAggregate": "false", "isGame": "true" if game_level else "false",
              "limit": -1, "start": 0, "cayenneExp": cayenne}
    rows = _get(f"{STATS}/{report}", params).get("data", [])
    if len(rows) >= ROW_CAP:
        raise FeedError(f"{report} hit the {ROW_CAP}-row cap for [{cayenne}] — "
                        f"the pull would be incomplete; use a smaller date chunk")
    return rows


def _game_types(include_playoffs: bool) -> str:
    return "(gameTypeId=2 or gameTypeId=3)" if include_playoffs else "gameTypeId=2"


def _chunks(start: date, end: date, days: int = 14):
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=days - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


SKATER_LOG_COLS = ["player_id", "player_name", "team", "opponent", "home_road",
                   "game_id", "game_date", "season", "position", "goals", "assists",
                   "points", "pp_goals", "pp_points", "shots", "toi", "pp_toi",
                   "sh_toi", "ev_toi", "shifts"]


def skater_game_logs(season: int, start: date, end: date,
                     include_playoffs: bool = True) -> pd.DataFrame:
    """One row per skater per game between two dates (inclusive).

    Two reports joined on player+game: `summary` carries scoring and shots, `timeonice`
    carries the PP / SH / EV split that tells us who is on the power play.
    """
    summ, toi = [], []
    gt = _game_types(include_playoffs)
    for a, b in _chunks(start, end):
        # gameDate<= is inclusive of that whole day (verified against a 2-day window)
        c = f'seasonId={season} and {gt} and gameDate>="{a}" and gameDate<="{b}"'
        summ += _stats("skater/summary", c)
        toi += _stats("skater/timeonice", c)
    if not summ:
        return pd.DataFrame(columns=SKATER_LOG_COLS)

    s = pd.DataFrame(summ)
    t = pd.DataFrame(toi)[["playerId", "gameId", "ppTimeOnIce", "shTimeOnIce",
                           "evTimeOnIce", "shifts"]]
    df = s.merge(t, on=["playerId", "gameId"], how="left")
    out = pd.DataFrame({
        "player_id": df["playerId"], "player_name": df["skaterFullName"],
        "team": df["teamAbbrev"], "opponent": df["opponentTeamAbbrev"],
        "home_road": df["homeRoad"], "game_id": df["gameId"],
        "game_date": df["gameDate"].astype(str).str[:10], "season": season,
        "position": df["positionCode"], "goals": df["goals"],
        "assists": df["assists"], "points": df["points"],
        "pp_goals": df["ppGoals"], "pp_points": df["ppPoints"], "shots": df["shots"],
        "toi": df["timeOnIcePerGame"] / 60.0,          # minutes
        "pp_toi": df["ppTimeOnIce"] / 60.0,
        "sh_toi": df["shTimeOnIce"] / 60.0,
        "ev_toi": df["evTimeOnIce"] / 60.0,
        "shifts": df["shifts"],
    })
    return out.drop_duplicates(["player_id", "game_id"])[SKATER_LOG_COLS]


HISTORY_COLS = ["player_id", "player_name", "team", "opponent", "game_id", "game_date",
                "season", "position", "goals", "assists", "points", "shots"]


def skater_history(season: int) -> pd.DataFrame:
    """A past regular season's scoring lines only (no ice time) - for head-to-head.

    Half the requests of skater_game_logs: head-to-head needs goals, assists, points
    and shots against an opponent, nothing about ice time. Pulled once per season and
    cached; a finished season never changes. The date window covers any regular season
    (they run October to mid-April); the 10,000-row cap is checked per 14-day chunk.
    """
    y = season // 10000
    rows = []
    for a, b in _chunks(date(y, 9, 20), date(y + 1, 5, 15)):
        c = f'seasonId={season} and gameTypeId=2 and gameDate>="{a}" and gameDate<="{b}"'
        rows += _stats("skater/summary", c)
    if not rows:
        return pd.DataFrame(columns=HISTORY_COLS)
    s = pd.DataFrame(rows)
    out = pd.DataFrame({
        "player_id": s["playerId"], "player_name": s["skaterFullName"],
        "team": s["teamAbbrev"], "opponent": s["opponentTeamAbbrev"],
        "game_id": s["gameId"], "game_date": s["gameDate"].astype(str).str[:10],
        "season": season, "position": s["positionCode"], "goals": s["goals"],
        "assists": s["assists"], "points": s["points"], "shots": s["shots"]})
    return out.drop_duplicates(["player_id", "game_id"])[HISTORY_COLS]


GOALIE_LOG_COLS = ["player_id", "player_name", "team", "opponent", "game_id",
                   "game_date", "season", "started", "shots_against", "saves",
                   "goals_against", "toi"]


def goalie_game_logs(season: int, include_playoffs: bool = True) -> pd.DataFrame:
    rows = _stats("goalie/summary", f"seasonId={season} and {_game_types(include_playoffs)}")
    if not rows:
        return pd.DataFrame(columns=GOALIE_LOG_COLS)
    d = pd.DataFrame(rows)
    return pd.DataFrame({
        "player_id": d["playerId"], "player_name": d["goalieFullName"],
        "team": d["teamAbbrev"], "opponent": d["opponentTeamAbbrev"],
        "game_id": d["gameId"], "game_date": d["gameDate"].astype(str).str[:10],
        "season": season, "started": d["gamesStarted"].fillna(0).astype(int),
        "shots_against": d["shotsAgainst"], "saves": d["saves"],
        "goals_against": d["goalsAgainst"], "toi": d["timeOnIce"] / 60.0,
    })[GOALIE_LOG_COLS]


TEAM_LOG_COLS = ["team_id", "team", "opponent", "home_road", "game_id", "game_date",
                 "season", "game_type", "win", "otl", "points", "goals_for",
                 "goals_against", "shots_for", "shots_against", "pp_goals_for",
                 "pp_opps", "pp_goals_against", "times_shorthanded"]


def team_game_logs(season: int, teams: pd.DataFrame,
                   include_playoffs: bool = True) -> pd.DataFrame:
    """One row per team per game: goals, shots and special teams, for and against."""
    c = f"seasonId={season} and {_game_types(include_playoffs)}"
    s = pd.DataFrame(_stats("team/summary", c))
    if s.empty:
        return pd.DataFrame(columns=TEAM_LOG_COLS)
    pp = pd.DataFrame(_stats("team/powerplay", c))[
        ["teamId", "gameId", "powerPlayGoalsFor", "ppOpportunities"]]
    pk = pd.DataFrame(_stats("team/penaltykill", c))[
        ["teamId", "gameId", "ppGoalsAgainst", "timesShorthanded"]]
    df = s.merge(pp, on=["teamId", "gameId"], how="left").merge(
        pk, on=["teamId", "gameId"], how="left")
    abbrev = dict(zip(teams["team_id"], teams["abbrev"]))
    out = pd.DataFrame({
        "team_id": df["teamId"], "team": df["teamId"].map(abbrev),
        "opponent": df["opponentTeamAbbrev"], "home_road": df["homeRoad"],
        "game_id": df["gameId"], "game_date": df["gameDate"].astype(str).str[:10],
        "season": season, "game_type": df["gameId"].astype(str).str[4:6].astype(int),
        "win": df["wins"], "otl": df["otLosses"], "points": df["points"],
        "goals_for": df["goalsFor"], "goals_against": df["goalsAgainst"],
        # *PerGame on a single-game row is that game's count
        "shots_for": df["shotsForPerGame"], "shots_against": df["shotsAgainstPerGame"],
        "pp_goals_for": df["powerPlayGoalsFor"], "pp_opps": df["ppOpportunities"],
        "pp_goals_against": df["ppGoalsAgainst"],
        "times_shorthanded": df["timesShorthanded"],
    })
    return out.drop_duplicates(["team_id", "game_id"])[TEAM_LOG_COLS]


def season_bounds(team_logs: pd.DataFrame, season: int) -> tuple[str | None, str | None]:
    """First and last REGULAR-season game date the logs contain for a season."""
    reg = team_logs[(team_logs["season"] == season) & (team_logs["game_type"] == REGULAR)]
    if reg.empty:
        return None, None
    return reg["game_date"].min(), reg["game_date"].max()


def parse_date(s: str) -> date:
    return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()


def cached(path: Path, build, refresh: bool = False) -> pd.DataFrame:
    """Read a CSV if it is already on disk, otherwise build and save it.

    Only used for the PREVIOUS season, which can no longer change. The current season
    is always pulled fresh.
    """
    if path.exists() and not refresh:
        return pd.read_csv(path)
    df = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    return df
