"""FanDuel moneylines from The Odds API (the-odds-api.com) -- FanDuel ONLY.

Checked live 10/3 on the user's key:
  - one pull = 1 credit (markets=h2h x bookmakers=fanduel = 1 region) and returns
    EVERY upcoming game of that sport; a pull that returns no games costs nothing
  - GET /v4/sports is free and its headers report the credits left
  - CFB 54/54 and MLB 4/4 of the app's games matched by team name + start time

The free plan is 500 credits a month, so pulls are BUDGETED (`allowed`). The
plan's size is read from the credits left; nothing is hard-coded to a plan.
Credits reset on the 1st of each month at 12 AM UTC (the user's account page,
10/3), so a day's share is the credits left spread over the days until then.
"""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BASE = "https://api.the-odds-api.com/v4"
SPORT_KEYS = {
    "MLB": "baseball_mlb",
    "NFL": "americanfootball_nfl",
    "WNBA": "basketball_wnba",
    "NBA": "basketball_nba",
    "CFB": "americanfootball_ncaaf",
    "NHL": "icehockey_nhl",
}
# The Odds API keeps PRESEASON games under their own key (checked 10/4: the NBA preseason --
# UTAH @ DEN, GS @ LAC -- is only in basketball_nba_preseason; basketball_nba starts 10/20).
# A pull whose window holds no games costs nothing, so asking both keys is free off-season.
EXTRA_KEYS = {
    "NBA": ("basketball_nba_preseason",),
    "NFL": ("americanfootball_nfl_preseason",),
}

# --- budget -----------------------------------------------------------------
FLOOR = 10  # never spend below this many credits (kept back for manual checks)
MIN_GAP = timedelta(minutes=5)  # never pull the same sport twice inside 5 minutes
# a pull-down refresh (user 10/3: "everything, every time") only skips a sport pulled for a
# pull-down in the last minute -- two quick pull-downs never pay twice for the same lines
REFRESH_GAP = timedelta(minutes=1)


def next_reset(now: datetime) -> datetime:
    """The 1st of next month, 12 AM UTC (8 PM Eastern on the month's last evening)."""
    n = now.astimezone(timezone.utc)
    return datetime(n.year + (n.month == 12), n.month % 12 + 1, 1, tzinfo=timezone.utc)


def days_left(now: datetime) -> int:
    """Days of budget left in the credit month, counting today (the last day = 1)."""
    return max(1, math.ceil((next_reset(now) - now).total_seconds() / 86400))


# SPEND FASTER (user 10/5, after ATL @ NO's 8:05 PM grab was skipped by the old even daily
# split -- chosen over a paid plan): every 10-minute grab goes while credits last ABOVE a
# reserve that pays each remaining day's midnight grab until the reset. The month's last
# stretch may get only midnight grabs. MORNING_PER_DAY ~ MLB 2 + NHL 2 + NFL 2 + CFB 1 + NBA 1.
MORNING_PER_DAY = 8


def morning_reserve(now: datetime) -> int:
    """Credits kept for the midnight grab of every day after today, until the reset."""
    return MORNING_PER_DAY * (days_left(now) - 1)


def day_cap(credits: int, now: datetime) -> int:
    """What the 10-minute grabs may still spend from now: credits above FLOOR and the
    morning reserve. On the month's last day that is everything left (unused credits
    don't carry over)."""
    return max(0, credits - FLOOR - morning_reserve(now))


def allowed(kind: str, used_today: int, cap: int, credits: int | None) -> bool:
    """The two grabs (user 10/3: the morning, then only ten minutes before each game).
    The MORNING line always (while credits last); each 10-minute line while today's cap
    lasts. Free plan (500): cap ~16/day (more as the reset nears with credits to spare).
    A PULL-DOWN refresh from the app always (user 10/3 chose "everything, every time", told
    credits can run out; FLOOR still holds). Its credits are counted apart (`refreshUsed`) so
    they never use up the day's 10-minute grabs."""
    if credits is None or credits <= FLOOR:
        return False
    if kind in ("morning", "refresh", "fill"):  # "fill": per-sport count + gap kept by the puller
        return True
    return kind == "T-10" and used_today < cap


# --- API ----------------------------------------------------------------------
class OddsApiError(Exception):
    pass


def out_of_credits(e: Exception) -> bool:
    """The Odds API's answer when an account's credits are used up (HTTP 401
    OUT_OF_USAGE_CREDITS / "Usage quota has been reached"; 429 on some plans)."""
    s = str(e)
    return ("401" in s or "429" in s) and any(w in s.lower() for w in ("quota", "usage", "credits"))


@dataclass
class FdGame:
    event_id: str
    away: str  # The Odds API's full name, e.g. "Chicago White Sox"
    home: str
    start_utc: datetime
    ml_away: int
    ml_home: int
    book_at: str  # FanDuel's own last_update
    # MLB run line / NHL puck line (main spread, always ±1.5): (point, price) per team, or None
    sp_away: tuple[float, int] | None = None
    sp_home: tuple[float, int] | None = None


# 10/3 (user: option boxes "should just fill automatically"): the run line / puck line is
# FanDuel's MAIN spread, so the same per-sport pull can carry it for every game: +1 credit
# per pull instead of 1 credit per game (13 NHL puck-line boxes on 10/3 alone).
SPREAD_SPORTS = ("MLB", "NHL", "NFL")  # NFL added 10/4 (user: the NFL spread boxes stayed empty)


def _get(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "dk-splits-robot"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else "", dict(e.headers or {})


def _remaining(headers: dict) -> int | None:
    for k, v in headers.items():
        if k.lower() == "x-requests-remaining":
            try:
                return int(float(v))
            except ValueError:
                return None
    return None


def credits_left(key: str, get=_get) -> int | None:
    """Free call (GET /sports never costs a credit)."""
    status, _, headers = get(f"{BASE}/sports/?apiKey={urllib.parse.quote(key)}")
    if status != 200:
        raise OddsApiError(f"HTTP {status} on /sports (bad key?)")
    return _remaining(headers)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_events(payload) -> list[FdGame]:
    out = []
    for e in payload if isinstance(payload, list) else []:
        fd = next((b for b in e.get("bookmakers", []) if b.get("key") == "fanduel"), None)
        if not fd:
            continue
        h2h = next((m for m in fd.get("markets", []) if m.get("key") == "h2h"), None)
        if not h2h:
            continue
        price = {o.get("name"): o.get("price") for o in h2h.get("outcomes", [])}
        a, h = price.get(e.get("away_team")), price.get(e.get("home_team"))
        if not all(isinstance(x, (int, float)) and abs(x) >= 100 for x in (a, h)):
            continue
        spreads = next((m for m in fd.get("markets", []) if m.get("key") == "spreads"), None)
        sp = {}
        for o in (spreads or {}).get("outcomes", []):
            if isinstance(o.get("point"), (int, float)) and isinstance(o.get("price"), (int, float)) and abs(o["price"]) >= 100:
                sp[o.get("name")] = (float(o["point"]), int(o["price"]))
        out.append(
            FdGame(
                str(e["id"]), e["away_team"], e["home_team"],
                datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")),
                int(a), int(h), fd.get("last_update", ""),
                sp.get(e["away_team"]), sp.get(e["home_team"]),
            )
        )
    return out


def sport_keys(sport: str) -> tuple[str, ...]:
    return (SPORT_KEYS[sport],) + EXTRA_KEYS.get(sport, ())


def fetch(sport: str, key: str, frm: datetime, to: datetime, get=_get) -> tuple[list[FdGame], int | None]:
    """FanDuel moneylines (+ the main spread for MLB/NHL/NFL) for `sport` games starting in
    [frm, to]. 1 credit per market (h2h, + spreads); 0 if no games. The preseason key is
    asked too (free when it has no games); only the main key's failure is an error."""
    markets = "h2h,spreads" if sport in SPREAD_SPORTS else "h2h"
    q = urllib.parse.urlencode({
        "apiKey": key, "bookmakers": "fanduel", "markets": markets, "oddsFormat": "american",
        "dateFormat": "iso", "commenceTimeFrom": _iso(frm), "commenceTimeTo": _iso(to),
    })
    games: list[FdGame] = []
    left = None
    for i, sk in enumerate(sport_keys(sport)):
        status, body, headers = get(f"{BASE}/sports/{sk}/odds/?{q}")
        if status != 200:
            if i == 0:
                raise OddsApiError(f"HTTP {status}: {body[:160]}")
            continue  # an off-season preseason key: nothing to add
        games += parse_events(json.loads(body))
        left = _remaining(headers) if _remaining(headers) is not None else left
    return games, left


def events_listed(sport: str, key: str, frm: datetime, to: datetime, get=_get) -> list[tuple[str, str, datetime]]:
    """FREE (GET /events costs nothing): the games The Odds API lists for `sport` in [frm, to]
    as (away, home, start). Used before a paid fill-in pull: no listed game, no credit."""
    q = urllib.parse.urlencode({"apiKey": key, "dateFormat": "iso",
                                "commenceTimeFrom": _iso(frm), "commenceTimeTo": _iso(to)})
    out = []
    for i, sk in enumerate(sport_keys(sport)):
        status, body, _ = get(f"{BASE}/sports/{sk}/events/?{q}")
        if status != 200:
            if i == 0:
                raise OddsApiError(f"HTTP {status} on /events")
            continue
        for e in json.loads(body) if body else []:
            try:
                out.append((e["away_team"], e["home_team"],
                            datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))))
            except (KeyError, ValueError):
                continue
    return out
