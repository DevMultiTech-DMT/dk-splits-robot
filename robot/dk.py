"""Read the public DraftKings Network betting-splits page (no login, plain HTML).

Each game block shows, per market (Moneyline / Run Line or Spread / Total), one
row per side with the odds, % Handle (share of the MONEY) and % Bets (share of
the TICKETS). Facts checked live 9/26:
  - 10 games per page (?tb_page=N); past the last page DK repeats the last one
  - the title reads "AWAY @ HOME" but the rows list the HOME side first, so
    rows are matched by label, never by position
  - times are Eastern ("9/26, 01:10PM"); no year
  - MLB games drop off at first pitch; CFB games stay listed after kickoff
  - some CFB blowouts carry no Moneyline block (spread + total only)
"""
from __future__ import annotations

import html
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
URL = "https://dknetwork.draftkings.com/draftkings-sportsbook-betting-splits/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
EVENT_GROUPS = {"MLB": 84240, "NFL": 88808, "WNBA": 94682, "CFB": 87637, "NBA": 42648, "NHL": 42133}
MAX_PAGES = 12
BLOCK_MARKERS = (
    "access denied",
    "just a moment",
    "cf-browser-verification",
    "pardon our interruption",
    "request unsuccessful",
)
# DK market heading -> our key
MARKET_KEYS = {
    "moneyline": "moneyline",
    "run line": "spread",
    "spread": "spread",
    "puck line": "spread",
    "total": "total",
}


class Blocked(Exception):
    """The page did not load as the splits page (blocked, error, layout change)."""


@dataclass
class Side:
    label: str  # "DET Tigers" | "DET Tigers +1.5" | "Over 9"
    odds: str
    handle: int  # % of the money
    bets: int  # % of the tickets


@dataclass
class DkGame:
    event_id: str
    away: str  # DK's label, e.g. "PIT Pirates"
    home: str
    when_et: str  # "9/26, 01:10PM"
    markets: dict[str, list[Side]] = field(default_factory=dict)


_ROW = re.compile(
    r'tb-slipline[^>]*>([^<]+)</div>\s*<div class="flex-1">\s*<a[^>]*>\s*([^<]+?)\s*</a>\s*</div>'
    r'\s*<div class="flex-1">(\d+)%.*?<div class="flex-1">(\d+)%',
    re.S,
)
_TITLE = re.compile(
    r'tb-se-title.*?<a[^>]*href="[^"]*event/(\d+)[^"]*"[^>]*>(.*?)</a>.*?<span[^>]*>\s*(.*?)\s*</span>',
    re.S,
)


def _clean(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", html.unescape(s))
    return re.sub(r"\s+", " ", s).strip()


def parse_page(page_html: str) -> list[DkGame]:
    games: list[DkGame] = []
    for block in page_html.split('<div class="tb-se ')[1:]:
        t = _TITLE.search(block)
        if not t:
            continue
        title = _clean(t.group(2))
        if " @ " in title:
            away, home = (x.strip() for x in title.split(" @ ", 1))
        elif " vs " in title:
            away, home = (x.strip() for x in title.split(" vs ", 1))
        else:
            continue
        g = DkGame(event_id=t.group(1), away=away, home=home, when_et=_clean(t.group(3)))
        for chunk in block.split("tb-se-head")[1:]:
            name = re.search(r'<div class="flex-1">([^<]+)</div>', chunk)
            key = MARKET_KEYS.get(_clean(name.group(1)).lower()) if name else None
            if not key:
                continue
            g.markets[key] = [
                Side(_clean(r[0]), _clean(r[1]).replace("−", "-"), int(r[2]), int(r[3]))
                for r in _ROW.findall(chunk)
            ]
        games.append(g)
    return games


def fetch_page(event_group: int, page: int, edate: str = "today", timeout: int = 30) -> str:
    q = f"?tb_eg={event_group}&tb_edate={edate}&tb_emt=0&tb_page={page}"
    req = urllib.request.Request(URL + q, headers={"User-Agent": UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise Blocked(f"HTTP {e.code}") from e
    except Exception as e:  # DNS, timeout, reset
        raise Blocked(f"{type(e).__name__}: {e}") from e
    low = body.lower()
    if "tb_eg" not in body:
        hit = next((m for m in BLOCK_MARKERS if m in low), None)
        raise Blocked(hit or "not the splits page (layout changed?)")
    return body


def fetch_league(sport: str, edate: str = "today", fetch=fetch_page) -> list[DkGame]:
    """Every game DK lists for the league, all pages, de-duplicated by event id."""
    seen: set[str] = set()
    out: list[DkGame] = []
    for page in range(1, MAX_PAGES + 1):
        got = parse_page(fetch(EVENT_GROUPS[sport], page, edate))
        new = [g for g in got if g.event_id not in seen]
        if not new:  # past the last page DK repeats the last page
            break
        for g in new:
            seen.add(g.event_id)
        out += new
        if len(got) < 10:
            break
    return out


def when_to_utc(when_et: str, now_utc: datetime) -> datetime | None:
    """ "9/26, 01:10PM" (Eastern, no year) -> aware UTC datetime, year chosen closest to now."""
    m = re.match(r"(\d{1,2})/(\d{1,2}),\s*(\d{1,2}):(\d{2})\s*([AP]M)", when_et.strip(), re.I)
    if not m:
        return None
    mo, d, hh, mm, ap = int(m[1]), int(m[2]), int(m[3]) % 12, int(m[4]), m[5].upper()
    if ap == "PM":
        hh += 12
    best = None
    for y in (now_utc.year - 1, now_utc.year, now_utc.year + 1):
        try:
            cand = datetime(y, mo, d, hh, mm, tzinfo=ET).astimezone(timezone.utc)
        except ValueError:
            continue
        if best is None or abs(cand - now_utc) < abs(best - now_utc):
            best = cand
    return best
