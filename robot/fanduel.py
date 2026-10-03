"""FanDuel moneylines from The Odds API (the-odds-api.com) -- FanDuel ONLY.

Checked live 10/3 on the user's key:
  - one pull = 1 credit (markets=h2h x bookmakers=fanduel = 1 region) and returns
    EVERY upcoming game of that sport; a pull that returns no games costs nothing
  - GET /v4/sports is free and its headers report the credits left
  - CFB 54/54 and MLB 4/4 of the app's games matched by team name + start time

The free plan is 500 credits a month, so pulls are BUDGETED (`allowed`). The
plan's size is read from the credits left; nothing is hard-coded to a plan.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

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
SPREAD_DAYS = 30  # a day's cap = (credits left - FLOOR) / 30, fixed at the day's first pull
MIN_GAP = timedelta(minutes=8)  # one pull per sport refreshes ALL its games; don't repeat inside 8 min
REFRESH_EVERY = timedelta(minutes=25)


def day_cap(credits: int) -> int:
    return max(0, (credits - FLOOR) // SPREAD_DAYS)


def allowed(kind: str, used_today: int, cap: int, credits: int | None) -> bool:
    """Priority: the MORNING line always (while credits last); the 10-minute line next;
    the 15-minute line and the every-30-minute refresh only on a bigger plan.
    Free plan (500): cap ~16/day -> morning + T-10. 20K plan: cap ~666/day -> everything."""
    if credits is None or credits <= FLOOR:
        return False
    if kind == "morning":
        return True
    if used_today >= cap:
        return False
    if kind == "T-10":
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
        out.append(
            FdGame(
                str(e["id"]), e["away_team"], e["home_team"],
                datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")),
                int(a), int(h), fd.get("last_update", ""),
            )
        )
    return out


def fetch(sport: str, key: str, frm: datetime, to: datetime, get=_get) -> tuple[list[FdGame], int | None]:
    """FanDuel moneylines for `sport` games starting in [frm, to]. 1 credit (0 if none)."""
    q = urllib.parse.urlencode({
        "apiKey": key, "bookmakers": "fanduel", "markets": "h2h", "oddsFormat": "american",
        "dateFormat": "iso", "commenceTimeFrom": _iso(frm), "commenceTimeTo": _iso(to),
    })
    status, body, headers = get(f"{BASE}/sports/{SPORT_KEYS[sport]}/odds/?{q}")
    if status != 200:
        raise OddsApiError(f"HTTP {status}: {body[:160]}")
    return parse_events(json.loads(body)), _remaining(headers)
