"""Groq — plain-English reasons for the top picks.

The LLM does NOT rank anything. Scores come from propline/scoring.py; Groq only turns
an already-decided shortlist into readable sentences. That keeps the betting logic
inspectable and means a bad model day can never reorder the board.

Cost control: one batched request per run covering every pick, rather than one call
per player. Comfortably inside Groq's free tier.
"""

from __future__ import annotations

import json
import os
import re
import time

import pandas as pd
import requests

ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
# A small model on purpose: this job is "restate two supplied numbers in a sentence",
# not reasoning, and the larger models burn the free daily token quota within a single
# day of scheduled runs.
#
# Model history, because this WILL happen again — Groq retires models with no notice:
#   llama-3.3-70b-versatile  -> exhausted the daily quota, then retired
#   llama-3.1-8b-instant     -> retired 2026-08; every call 404'd mid-slate
#   openai/gpt-oss-20b       -> current
#
# If this 404s, list the models the key can actually see and pick the smallest
# instruction-following one:
#   GET https://api.groq.com/openai/v1/models
#
# Rejected alternatives when choosing this one: qwen3.6-27b could not hold to the JSON
# schema (400s), and groq/compound-mini was accurate but cost ~2x the tokens for the
# same two sentences.
MODEL = "openai/gpt-oss-20b"
TIMEOUT = 120
# Twelve, established by testing against a real slate rather than guessed.
#
# The cap is the model's OUTPUT length, not the input. A batch of 25 real picks is
# ~8,000 characters in and needs 25 sentences back; gpt-oss-20b silently returns an
# empty completion, which Groq reports as `json_validate_failed` with an empty
# `failed_generation` — a confusing error that reads like a prompt problem and is not
# one. Synthetic test rows are far shorter than real ones, so 25 passes in a toy test
# and fails in production. Re-measure with real data if the model ever changes.
MAX_PER_CALL = 12

FIELD_GLOSSARY = """Field meanings (all numbers are real and may be quoted):
- prop: which bet the pick is for - sog (shots on goal), points, goals, assists,
  ppp (power-play points)
- opponent: the team he plays tonight
- sog_pg_recent: shots on goal per game over his last 10 games
- shots60 / goals60 / assists60 / points60: per 60 minutes of ice time, this season
  blended with last season
- goals_recent / assists_recent / ppp_recent: totals over his last 10 games
- points_pg_recent: points per game over his last 10 games
- ppp_pg: power-play points per game, season
- sh_pct: his shooting percentage, already a percent (12.5 means 12.5%)
- toi_recent: minutes per game over his last 10; pp_toi_recent: power-play minutes
  per game over his last 10
- toi_bump / pp_toi_bump: EXTRA minutes he is expected to pick up tonight because a
  teammate is out; bump_from names that teammate
- opp_sa_pos: shots on goal the opponent allows per game to players at his position
  (forwards or defensemen)
- opp_ga: goals the opponent allows per game; team_gf: goals his own team scores per game
- opp_goalie / opp_goalie_sv: the goalie expected in net for the opponent and his save
  percentage (.900 is about average; lower is weaker)
- opp_goalie_status: "projected/high", "projected/lean" or "confirmed/confirmed". If it
  starts with "projected", call him the PROJECTED or likely starter, never "starting"
- opp_pen_taken: times per game the opponent goes shorthanded (gives power plays)
- opp_pk_pct: the opponent's penalty-kill percent, already a percent
- ixg60: expected goals per 60 minutes from the QUALITY of his shots (our own
  shot-quality model: distance, angle, shot type, strength); ~0.9+ is a top shooter
- hd_pg: dangerous shots per game - attempts from the most dangerous fifth of locations
- recent_games: how many games the recent numbers cover (fewer = less reliable)"""

SYSTEM = (
    "You write one-sentence explanations for hockey player prop shortlists.\n"
    "The picks were ALREADY ranked by a statistical model. Never re-rank them, never "
    "contradict the numbers, never invent a statistic that is not in the input.\n\n"
    + FIELD_GLOSSARY +
    "\n\nRules:\n"
    "- ONE complete sentence per pick, 12-22 words.\n"
    "- Name the player, then cite two concrete numbers relevant to THAT prop as evidence.\n"
    "- If toi_bump or pp_toi_bump is present, mention the extra minutes and whose they are.\n"
    "- Never output a bare field name or a raw key:value pair.\n"
    "- Plain language. No hype, no betting advice, no guarantees, no 'lock' or 'smash'.\n\n"
    # NB: deliberately a made-up player, so a real pick can never be answered by
    # echoing the example.
    "Example input:  {\"id\": 0, \"prop\": \"sog\", \"player_name\": \"Sample Skater\", "
    "\"opponent\": \"ABC\", \"sog_pg_recent\": 3.6, \"opp_sa_pos\": 21.4}\n"
    "Example output: {\"id\": 0, \"text\": \"Sample Skater is averaging 3.6 shots a "
    "game over his last ten, and ABC allow 21.4 shots a night to forwards.\"}\n\n"
    "Return strict JSON: {\"rationales\": [{\"id\": <id>, \"text\": \"...\"}]}\n\n"
    "STRICT WORDING:\n"
    "- Per-60 rates are per 60 minutes of ICE TIME, not per game. Never write 'per game' "
    "for shots60, goals60, assists60 or points60.\n"
    "- A projected goalie is not confirmed. Never write that he 'starts' or 'is in net'.\n"
    "- If recent_games is below 7, you MUST say the sample is small."
)


# Game totals have no player - the player prompt above would make the model complain
# about missing data and then return empty completions (MLB lesson).
TOTALS_SYSTEM = (
    "You write one-sentence explanations for hockey TOTAL GOALS picks - combined goals "
    "by both teams in one game. There is deliberately no player in the input - never ask "
    "for one, and never say data is missing.\n\n"
    "Field meanings (real numbers, may be quoted):\n"
    "- matchup: away team @ home team\n"
    "- home_gf_pg / away_gf_pg: goals that team scores per game\n"
    "- home_ga_pg / away_ga_pg: goals that team allows per game\n"
    "- home_goalie / away_goalie with home_goalie_sv / away_goalie_sv: the goalie expected "
    "in net for that team and his save percentage (.900 is about average)\n"
    "- goalies_status: 'projected' or 'confirmed' for away / home. A projected goalie is "
    "the LIKELY starter - never write that he starts\n"
    "- missing_names: regulars out tonight, if any\n\n"
    "Rules:\n"
    "- ONE complete sentence per pick, 12-22 words.\n"
    "- Name the teams, then give two reasons from the fields provided.\n"
    "- Plain language. No hype, no betting advice, no guarantees.\n\n"
    "Example output: {\"id\": 0, \"text\": \"Both ABC and XYZ allow over 3.4 goals a "
    "game, and XYZ's projected goalie carries an .887 save percentage.\"}\n\n"
    "Return strict JSON: {\"rationales\": [{\"id\": <id>, \"text\": \"...\"}]}"
)



# Phase 2 game and team boards. One prompt covers all of them; `prop` says which board.
# The strength gap is an internal index, so it is banded into the word in `edge` before
# the model sees it (MLB lesson: told not to quote an index, the model quoted it anyway).
GAME_SYSTEM = (
    "You write one-sentence explanations for hockey GAME and TEAM picks that were "
    "ALREADY ranked by a statistical model. Never re-rank them, never contradict the "
    "numbers, never invent a statistic that is not in the input, never say data is "
    "missing.\n\n"
    "prop says which board the pick is on:\n"
    "- team_goals: goals this team scores tonight\n"
    "- team_sog / game_sog: shots on goal by this team / by both teams\n"
    "- team_ppg / game_ppg: power-play goals by this team / by both teams\n"
    "- moneyline: which side is the stronger team tonight\n"
    "- puck_line: the stronger side winning by 2 or more\n\n"
    "Field meanings (real numbers, may be quoted):\n"
    "- gf / opp_ga: goals per game this team scores / its opponent allows\n"
    "- sf / opp_sa: shots per game this team takes / its opponent allows\n"
    "- pp_pct: power-play percent, already a percent (25.0 means 25%); opp_pen_taken: times per game the "
    "opponent goes shorthanded; opp_pk_pct: opponent penalty-kill percent, already a percent\n"
    "- opp_goalie / opp_goalie_sv: the goalie this team SHOOTS AT tonight; a low save "
    "percentage HELPS this team score - never call it a defensive edge for this team\n"
    "- pick_goalie / dog_goalie: each side's own goalie. Save percentages: .900 is about "
    "average. Goalies are PROJECTED, so call them the likely or projected goalie, never "
    "say he starts\n"
    "- home_ppg / away_ppg: power-play goals per game for that team\n"
    "- pick / dog: on moneyline and puck_line, pick is the side the model rates "
    "STRONGER and dog the other side. Never describe dog as favoured\n"
    "- pick_gd / dog_gd: goal difference per game (goals for minus against); pick_gf: "
    "goals per game the pick scores; dog_ga: goals per game the dog allows\n"
    "- edge: Slight, Moderate or Strong - how much stronger the pick is. Use the word; "
    "there is no number for it\n"
    "- pick_b2b / dog_b2b: true if that team played last night\n"
    "- market_agrees: whether sportsbooks also favour the pick (yes / no)\n"
    "- missing_names: regulars out tonight\n"
    "- home_team / away_team name the two sides. A home_* number belongs to "
    "home_team and an away_* number to away_team - always name the team, never "
    "'the home team'. home_opp_pen_taken is how often away_team goes shorthanded, "
    "away_opp_pen_taken how often home_team does\n\n"
    "Rules:\n"
    "- ONE complete sentence per pick, 12-22 words.\n"
    "- Name the team or matchup, then give two concrete reasons from the fields.\n"
    "- Never state or imply a win probability or a predicted score.\n"
    "- Plain language. No hype, no betting advice, no guarantees.\n\n"
    "Example output: {\"id\": 0, \"text\": \"ABC take 32.1 shots a game and face an XYZ "
    "side allowing 31.4, the busiest shot matchup tonight.\"}\n\n"
    "Return strict JSON: {\"rationales\": [{\"id\": <id>, \"text\": \"...\"}]}"
)


def _payload(rows: list[dict]) -> str:
    return json.dumps({"picks": rows}, default=str)


def label_internal_indexes(df: pd.DataFrame, mapping: dict[str, tuple[str, str, str]]
                          ) -> pd.DataFrame:
    """Replace arbitrary internal indexes with plain-English bands.

    Telling the model "don't quote this number" does not reliably work — it quoted
    "0.674 combined offense" anyway. So the number never gets sent: each index is
    converted to a word based on where it sits across today's slate, and only the word
    is passed on. Deterministic, and impossible for the model to misread.

    mapping: {column: (low_label, mid_label, high_label)}
    """
    out = df.copy()
    for col, (lo, mid, hi) in mapping.items():
        if col not in out.columns:
            continue
        pct = out[col].rank(pct=True)
        out[col + "_desc"] = pct.map(
            lambda p: lo if pd.isna(p) or p < 0.34 else (mid if p < 0.67 else hi))
    return out


RATE_LIMIT_RETRIES = 6

# Groq's free tier caps TOKENS PER MINUTE, not per request. Eight categories fired two
# seconds apart all land inside the same minute and blow the ceiling together — which
# is how Total Bases ended up with no rationale at all while its neighbours had 25.
# Spreading the calls across the minute keeps each one under the limit, at the cost of
# about a minute on a run that already takes several.
PAUSE_BETWEEN_CALLS = 7.0

# Groq reports two different 429s with the same status code: a per-MINUTE limit that
# clears in seconds, and a per-DAY quota that clears in tens of minutes. Waiting out
# the daily one would stall a scheduled run past its 45-minute timeout, five times a
# day — so anything longer than this is treated as "no rationale today" instead.
# Picks are still fully ranked and usable; only the written sentence is missing.
MAX_WAIT_SECONDS = 90


def _retry_after(resp) -> float:
    """How long Groq wants us to wait. It says so in the header and the message."""
    hdr = resp.headers.get("retry-after")
    if hdr:
        try:
            return float(hdr)
        except ValueError:
            pass
    m = re.search(r"try again in ([\d.]+)s", resp.text)
    return float(m.group(1)) if m else 5.0


def _call(rows: list[dict], api_key: str, system: str = None) -> dict[int, str]:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system or SYSTEM},
            {"role": "user", "content": _payload(rows)},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    for attempt in range(1, RATE_LIMIT_RETRIES + 1):
        r = requests.post(ENDPOINT, headers={"Authorization": f"Bearer {api_key}"},
                          json=body, timeout=TIMEOUT)
        if r.status_code == 429:
            wait = _retry_after(r) + 0.5
            if wait > MAX_WAIT_SECONDS:
                raise RuntimeError(
                    f"daily quota reached (Groq asked for {wait / 60:.0f} min) — "
                    f"skipping rationale text for this run"
                )
            if attempt < RATE_LIMIT_RETRIES:
                print(f"    rate limited, waiting {wait:.1f}s")
                time.sleep(wait)
                continue
        if not r.ok:
            raise RuntimeError(f"groq {r.status_code}: {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        data = json.loads(content)
        return {int(item["id"]): item.get("text", "") for item in data.get("rationales", [])}
    raise RuntimeError("groq: rate limited after retries")


def _own(got: dict[int, str], sent: list[dict]) -> dict[int, str]:
    """Keep only answers whose id was in the batch that produced them."""
    ids = {r["id"] for r in sent}
    # the model likes non-breaking hyphens ("power‑play"), which break CSV/Excel search
    return {k: v.replace("‑", "-") for k, v in got.items() if k in ids and v}


def add_rationales(df: pd.DataFrame, fields: list[str], label: str,
                   top_n: int = 15, api_key: str | None = None,
                   system: str | None = None) -> pd.DataFrame:
    """Attach a `rationale` column to the top N rows of a scored frame.

    Only the shortlist gets sent — there is no value in explaining pick #180, and it
    keeps the request small. Everything below top_n simply has no rationale.
    """
    df = df.copy()
    if "rationale" not in df.columns:
        df["rationale"] = None
    if df.empty:
        return df

    api_key = (api_key or os.getenv("GROQ_API_KEY") or "").strip()  # see db.env
    if not api_key:
        print("  WARN  GROQ_API_KEY missing — picks will have no written reasons")
        return df

    top = df.sort_values("score", ascending=False).head(top_n)
    rows = []
    for i, (_, r) in enumerate(top.iterrows()):
        item = {"id": i, "prop": label}
        for f in fields:
            if f in r.index and pd.notna(r[f]):
                v = r[f]
                if isinstance(v, (int, float)):
                    # keep whole numbers whole, or the model writes "a 104.0 park"
                    item[f] = int(v) if float(v).is_integer() else round(float(v), 3)
                else:
                    item[f] = str(v)
        rows.append(item)

    # Degrade gracefully. Groq is the least reliable dependency in the pipeline — models
    # get retired, quotas bite, and this one intermittently returns an empty completion —
    # so a failure must cost only the sentences it actually lost. Previously one bad
    # chunk returned early and discarded every rationale already collected for that
    # category, turning a partial failure into a total one.
    texts: dict[int, str] = {}
    split = 0
    for i in range(0, len(rows), MAX_PER_CALL):
        chunk = rows[i:i + MAX_PER_CALL]
        # Ids are already positions in the WHOLE shortlist (see the loop above), so
        # they are used as returned. Adding the chunk offset again — as the MLB copy of
        # this code does — pinned sentences to the wrong player after any split, and
        # pushed every chunk past the first off the end of the list. Ids outside the
        # chunk are discarded rather than trusted.
        try:
            got = _call(chunk, api_key, system)
            texts.update(_own(got, chunk))
        except Exception as exc:  # noqa: BLE001 — never let this break the pipeline
            # Retry at half size: an empty completion is usually the model running out
            # of output room, which a smaller batch fixes.
            split += 1
            half = max(1, len(chunk) // 2)
            for j in range(0, len(chunk), half):
                sub = chunk[j:j + half]
                try:
                    got = _call(sub, api_key, system)
                    texts.update(_own(got, sub))
                except Exception:
                    print(f"  WARN  {label}: {len(sub)} picks unexplained ({exc})")
                time.sleep(PAUSE_BETWEEN_CALLS)
        time.sleep(PAUSE_BETWEEN_CALLS)

    if texts:
        note = f", {split} chunk(s) split" if split else ""
        print(f"  ok    {label}: {len(texts)}/{len(rows)} explained{note}")

    for pos, idx in enumerate(top.index):
        if pos in texts:
            df.at[idx, "rationale"] = texts[pos]
    return df
