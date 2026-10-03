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

# --- budget -----------------------------------------------------------------
FLOOR = 10  # never spend below this many credits (kept back for manual checks)
MIN_GAP = timedelta(minutes=8)  # one pull per sport refreshes ALL its games; don't repeat inside 8 min
REFRESH_EVERY = timedelta(minutes=25)


def next_reset(now: datetime) -> datetime:
    """The 1st of next month, 12 AM UTC (8 PM Eastern on the month's last evening)."""
    n = now.astimezone(timezone.utc)
    return datetime(n.year + (n.month == 12), n.month % 12 + 1, 1, tzinfo=timezone.utc)


def days_left(now: datetime) -> int:
    """Days of budget left in the credit month, counting today (the last day = 1)."""
    return max(1, math.ceil((next_reset(now) - now).total_seconds() / 86400))


def day_cap(credits: int, now: datetime) -> int:
    """Today's share: (credits left - FLOOR) spread over the days until the reset. On the
    month's last day that is everything left -- unused credits don't carry over."""
    return max(0, (credits - FLOOR) // days_left(now))


def allowed(kind: str, used_today: int, cap: int, credits: int | None) -> bool:
    """Priority: the MORNING line always (while credits last); the 10-minute line next;
    the 15-minute line and the every-30-minute refresh only on a bigger plan.
    Free plan (500): cap ~16/day -> morning + T-10 (more as the reset nears with credits
    to spare). 20K plan: cap ~650/day -> everything."""
    if credits is None or credits <= FLOOR:
        return False
    if kind == "morning":
        return True
    if used_today >= cap:
        return False
    if kind in ("T-10", "spreads"):
        return True
    if kind == "T-15":
        return cap >= 40
    if kind == "refresh":
        return cap >= 100
    return False


# --- API ----------------------------------------------------------------------
class OddsApiError(Exception):
    pass


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
SPREAD_SPORTS = ("MLB", "NHL")


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


def fetch(sport: str, key: str, frm: datetime, to: datetime, get=_get) -> tuple[list[FdGame], int | None]:
    """FanDuel moneylines (+ the main run/puck line for MLB/NHL) for `sport` games starting
    in [frm, to]. 1 credit per market (h2h, + spreads for MLB/NHL); 0 if no games."""
    markets = "h2h,spreads" if sport in SPREAD_SPORTS else "h2h"
    q = urllib.parse.urlencode({
        "apiKey": key, "bookmakers": "fanduel", "markets": markets, "oddsFormat": "american",
        "dateFormat": "iso", "commenceTimeFrom": _iso(frm), "commenceTimeTo": _iso(to),
    })
    status, body, headers = get(f"{BASE}/sports/{SPORT_KEYS[sport]}/odds/?{q}")
    if status != 200:
        raise OddsApiError(f"HTTP {status}: {body[:160]}")
    return parse_events(json.loads(body)), _remaining(headers)
