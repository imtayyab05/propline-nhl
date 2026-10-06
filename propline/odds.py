"""Market lines for the game boards — moneyline, puck line, total, team totals.

Client decision (4 Oct 2026): GAME LINES ONLY, on The Odds API's free 500 credits a
month, "until we move to a subscription selling service". No player prop lines.

CREDIT BUDGET — read before changing anything here
--------------------------------------------------
Measured against the live API and its docs on 4 Oct 2026:
  /sports, /events            free
  /odds h2h,spreads,totals    3 credits, the whole slate in one call
  /events/{id}/odds team_totals   1 credit PER GAME (only markets returned are charged;
                                   an empty response costs nothing)
Plan: game lines on the morning and pre-game runs (2 x 3 x ~30 days = ~180), team
totals on the pre-game run only (~7 games x ~30 days = ~210). ~390 a month, under 500
with room for a few manual pulls. Every other run reuses the lines stored in Supabase
(table market_lines), so they never cost a credit.

The budget is checked (free call) before spending, and a run that would dip under
RESERVE skips the pull and says so — the key must never die silently mid-month.

Consensus: the median price across US books at the most-quoted line. One book can be
stale or shaded; the client compares our rating against "the market", not one book.
His own book (Island Luck, Bahamas) is not in this feed, so these are a reference.
"""

from __future__ import annotations

import os
import unicodedata
from collections import Counter
from datetime import datetime, timezone

import pandas as pd
import requests

BASE = "https://api.the-odds-api.com/v4"
SPORT = "icehockey_nhl"
REGION = "us"
TIMEOUT = 45
GAME_MARKETS = "h2h,spreads,totals"
RESERVE = 40          # never spend below this many credits left this month

LINE_COLS = ["game_id", "market", "subject", "side", "point", "price", "books",
             "fetched_at"]


class OddsError(RuntimeError):
    pass


def _key() -> str | None:
    return (os.getenv("ODDS_API_KEY") or "").strip() or None  # see db.env


def _get(path: str, **params):
    key = _key()
    if not key:
        raise OddsError("ODDS_API_KEY is not set")
    params["apiKey"] = key
    r = requests.get(f"{BASE}{path}", params=params, timeout=TIMEOUT)
    if r.status_code == 401:
        raise OddsError("odds API rejected the key")
    if r.status_code == 429:
        raise OddsError("odds API quota exhausted for this key")
    r.raise_for_status()
    left = r.headers.get("x-requests-remaining")
    return r.json(), (int(float(left)) if left not in (None, "") else None)


def credits_left() -> int | None:
    """Free call: the /sports endpoint does not count against the quota."""
    _, left = _get("/sports")
    return left


# --- names and prices ----------------------------------------------------------------

def norm_name(n) -> str:
    """'St Louis Blues' and 'St. Louis Blues', 'Montreal' and 'Montréal' match."""
    s = unicodedata.normalize("NFKD", str(n)).encode("ascii", "ignore").decode()
    return " ".join(s.replace(".", " ").lower().split())


def _to_dec(a: float) -> float:
    return 1 + a / 100 if a > 0 else 1 + 100 / abs(a)


def _to_american(d: float) -> int:
    return int(round((d - 1) * 100)) if d >= 2 else int(round(-100 / (d - 1)))


def _median_price(prices: list[float]) -> int | None:
    """Median in decimal odds, back to American. A plain median of American prices
    misbehaves across the -100/+100 boundary (-110 and +110 'average' to 0)."""
    if not prices:
        return None
    dec = sorted(_to_dec(p) for p in prices)
    mid = len(dec) // 2
    m = dec[mid] if len(dec) % 2 else (dec[mid - 1] + dec[mid]) / 2
    return _to_american(m)


# --- fetch ------------------------------------------------------------------------------

def fetch(mode: str, schedule: pd.DataFrame, teams: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Pull lines for tonight's games. mode: 'game' (3 credits) or 'full' (+team totals).

    Returns (lines in long form, a one-line note for the run log).
    """
    if schedule.empty:
        return pd.DataFrame(columns=LINE_COLS), "no games"
    left = credits_left()
    need = 3 + (len(schedule) if mode == "full" else 0)
    if left is not None and left - 3 < RESERVE:
        return pd.DataFrame(columns=LINE_COLS), f"SKIPPED: only {left} credits left"
    if left is not None and left - need < RESERVE:
        mode = "game"          # keep the cheap pull, drop the per-game one

    events, left = _get(f"/sports/{SPORT}/odds", regions=REGION, markets=GAME_MARKETS,
                        oddsFormat="american")
    by_name = {norm_name(n): a for n, a in zip(teams["team_name"], teams["abbrev"])}
    games = {(r.home_team, r.away_team): r.game_id for r in schedule.itertuples()}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")   # API format

    rows, matched, live = [], {}, 0
    for ev in events:
        # /odds also serves IN-PLAY lines for games already under way (a 4.5 total on a
        # game that opened at 6.5, seen 4 Oct 2026). Those are not the pre-game market
        # our rating is set against, so started games keep the line stored earlier.
        if ev.get("commence_time", "") <= now:
            live += 1
            continue
        h, a = by_name.get(norm_name(ev["home_team"])), by_name.get(norm_name(ev["away_team"]))
        gid = games.get((h, a))
        if gid is None:
            continue           # another day's game, or a name we cannot map
        matched[gid] = ev["id"]
        name_to_abbr = {ev["home_team"]: h, ev["away_team"]: a}
        rows += _game_rows(gid, ev, name_to_abbr, now)

    note = f"game lines for {len(matched)}/{len(schedule)} games"
    if live:
        note += f" ({live} already started, kept earlier lines)"
    if mode == "full":
        tt = 0
        for gid, ev_id in matched.items():
            ev, left = _get(f"/sports/{SPORT}/events/{ev_id}/odds", regions=REGION,
                            markets="team_totals", oddsFormat="american")
            h, a = schedule.loc[schedule.game_id == gid, ["home_team", "away_team"]].iloc[0]
            got = _team_total_rows(gid, ev, {ev.get("home_team"): h, ev.get("away_team"): a}, now)
            tt += bool(got)
            rows += got
        note += f", team totals for {tt}"
    note += f"; {left} credits left"
    return pd.DataFrame(rows, columns=LINE_COLS), note


def _game_rows(gid, ev, name_to_abbr, now) -> list[dict]:
    ml, sp, tot = {}, [], []
    books = ev.get("bookmakers", [])
    for b in books:
        for m in b["markets"]:
            if m["key"] == "h2h":
                for o in m["outcomes"]:
                    ml.setdefault(name_to_abbr.get(o["name"]), []).append(o["price"])
            elif m["key"] == "spreads":
                sp.append({name_to_abbr.get(o["name"]): (o.get("point"), o["price"])
                           for o in m["outcomes"]})
            elif m["key"] == "totals":
                tot.append({o["name"].lower(): (o.get("point"), o["price"])
                            for o in m["outcomes"]})
    out = []
    for team, prices in ml.items():
        if team:
            out.append(dict(game_id=gid, market="moneyline", subject=team, side="",
                            point=None, price=_median_price(prices), books=len(prices),
                            fetched_at=now))
    out += _consensus(gid, "puck_line", sp, now)
    out += _consensus(gid, "total", tot, now)
    return out


def _consensus(gid, market, per_book: list[dict], now) -> list[dict]:
    """Most-quoted line, median price at that line, for each side."""
    if not per_book:
        return []
    sides = sorted({k for d in per_book for k in d if k})
    first = sides[0]
    pts = Counter(d[first][0] for d in per_book if first in d and d[first][0] is not None)
    if not pts:
        return []
    main = pts.most_common(1)[0][0]
    at_main = [d for d in per_book if first in d and d[first][0] == main]
    rows = []
    for s in sides:
        prices = [d[s][1] for d in at_main if s in d]
        point = next((d[s][0] for d in at_main if s in d), None)
        subject, side = (s, "") if market == "puck_line" else ("game", s)
        rows.append(dict(game_id=gid, market=market, subject=subject, side=side,
                         point=point, price=_median_price(prices), books=len(prices),
                         fetched_at=now))
    return rows


def _team_total_rows(gid, ev, name_to_abbr, now) -> list[dict]:
    per_team: dict[str, list[dict]] = {}
    for b in ev.get("bookmakers", []):
        for m in b["markets"]:
            if m["key"] != "team_totals":
                continue
            book: dict[str, dict] = {}
            for o in m["outcomes"]:
                t = name_to_abbr.get(o.get("description"))
                if t:
                    book.setdefault(t, {})[o["name"].lower()] = (o.get("point"), o["price"])
            for t, d in book.items():
                per_team.setdefault(t, []).append(d)
    rows = []
    for t, lst in per_team.items():
        for r in _consensus(gid, "team_total", lst, now):
            rows.append({**r, "subject": t})
    return rows


# --- attach to boards -----------------------------------------------------------------

def _pick(lines: pd.DataFrame, gid, market, subject=None, side=None):
    m = lines[(lines.game_id == gid) & (lines.market == market)]
    if subject is not None:
        m = m[m.subject == subject]
    if side is not None:
        m = m[m.side == side]
    return m.iloc[0] if len(m) else None


def attach(boards: dict[str, pd.DataFrame], totals: pd.DataFrame,
           lines: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Put the market's number beside our rating on each board that has a market."""
    if lines is None or lines.empty:
        return boards, totals
    lines = lines.copy()
    lines["game_id"] = pd.to_numeric(lines["game_id"]).astype("int64")

    if totals is not None and not totals.empty:
        t = totals.copy()
        ov = [_pick(lines, g, "total", side="over") for g in t.game_id]
        un = [_pick(lines, g, "total", side="under") for g in t.game_id]
        t["market_total"] = [o["point"] if o is not None else None for o in ov]
        t["over_price"] = [o["price"] if o is not None else None for o in ov]
        t["under_price"] = [u["price"] if u is not None else None for u in un]
        totals = t

    if "team_goals" in boards:
        b = boards["team_goals"].copy()
        ov = [_pick(lines, g, "team_total", s, "over") for g, s in zip(b.game_id, b.team)]
        un = [_pick(lines, g, "team_total", s, "under") for g, s in zip(b.game_id, b.team)]
        b["market_team_total"] = [o["point"] if o is not None else None for o in ov]
        b["tt_over_price"] = [o["price"] if o is not None else None for o in ov]
        b["tt_under_price"] = [u["price"] if u is not None else None for u in un]
        boards["team_goals"] = b

    for key in ("moneyline", "puck_line"):
        if key not in boards:
            continue
        b = boards[key].copy()
        pm = [_pick(lines, g, "moneyline", p) for g, p in zip(b.game_id, b.pick)]
        dm = [_pick(lines, g, "moneyline", d) for g, d in zip(b.game_id, b.dog)]
        b["pick_ml"] = [r["price"] if r is not None else None for r in pm]
        b["dog_ml"] = [r["price"] if r is not None else None for r in dm]
        # the market's favourite is the side with the shorter price
        fav = []
        for p, d, pick, dog in zip(b["pick_ml"], b["dog_ml"], b["pick"], b["dog"]):
            if p is None or d is None or p != p or d != d:
                fav.append(None)
            else:
                fav.append(pick if _to_dec(p) < _to_dec(d) else dog)
        b["market_fav"] = fav
        b["market_agrees"] = [None if f is None else ("yes" if f == p else "no")
                              for f, p in zip(fav, b["pick"])]
        pl = [_pick(lines, g, "puck_line", p) for g, p in zip(b.game_id, b.pick)]
        b["pick_pl_point"] = [r["point"] if r is not None else None for r in pl]
        b["pick_pl_price"] = [r["price"] if r is not None else None for r in pl]
        boards[key] = b
    return boards, totals
