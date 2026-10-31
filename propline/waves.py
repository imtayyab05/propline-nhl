"""Game-day wave runs (1 Nov 2026) - the Python twin of netlify/lib/waves.mjs.

The Netlify function decides when to dispatch; this copy lets the health check work out
which wave runs a slate SHOULD have had, and lets us simulate a past day. Keep the two
identical: same constants, same three steps (see the .mjs header).
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone

TICK_MIN = 15
LEAD_MIN = 90
MIN_LEAD_MIN = 30
WAVE_SPAN_MIN = 30
WAVES_FROM = date(2026, 11, 1)      # first slate scheduled with waves (no midday / late)


def fixed_runs(day: str) -> dict[str, datetime]:
    d = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    return {"scheduled_morning": d + timedelta(hours=12, minutes=7),
            "scheduled_pregame": d + timedelta(hours=22, minutes=37)}


def plan_waves(day: str, starts: list[datetime]) -> list[dict]:
    s = sorted(set(t for t in starts if t is not None))
    fixed = list(fixed_runs(day).values())
    waves: list[dict] = []
    for t in s:
        if waves and t - waves[-1]["anchor"] <= timedelta(minutes=WAVE_SPAN_MIN):
            waves[-1]["starts"].append(t)
        else:
            waves.append({"anchor": t, "starts": [t]})
    tick = TICK_MIN * 60
    for w in waves:
        early = w["anchor"] - timedelta(minutes=LEAD_MIN)
        w["dispatch"] = datetime.fromtimestamp(math.ceil(early.timestamp() / tick) * tick,
                                               tz=timezone.utc)
        cover = [f for f in fixed
                 if early <= f <= w["anchor"] - timedelta(minutes=MIN_LEAD_MIN)]
        w["skip"] = "covered by a fixed run" if cover else None
    return waves
