"""Official schedules -- the SAME game ids the PropEdge app keys its cards on.

MLB  -> statsapi.mlb.com gamePk           (app: services/MLBStatsService.ts)
NFL / WNBA / NBA / CFB -> ESPN event id   (app: services/*EspnService.ts)

The app's card key is `${sport}-${gameId}`; the robot uses the same key as the
Firestore document id, so a number can only ever land on the game it was
matched to.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime

MLB_SCHEDULE = "https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={d}&hydrate=team"
# site.api can 403 some datacenter IPs; site.web.api serves the same payload
ESPN_HOSTS = ("https://site.api.espn.com", "https://site.web.api.espn.com")
ESPN_PATHS = {
    "NFL": "/apis/site/v2/sports/football/nfl/scoreboard?dates={d}",
    "WNBA": "/apis/site/v2/sports/basketball/wnba/scoreboard?dates={d}",
    "NBA": "/apis/site/v2/sports/basketball/nba/scoreboard?dates={d}",
    "CFB": "/apis/site/v2/sports/football/college-football/scoreboard?dates={d}&groups=80&limit=300",
}
SPORTS = ("MLB", "NFL", "WNBA", "NBA", "CFB")


@dataclass
class Team:
    nickname: str  # "Tigers", "White Sox", "49ers" (pro matching key)
    names: tuple[str, ...]  # every raw name variant the feed gives (CFB matching)
    abbr: str


@dataclass
class OfficialGame:
    sport: str
    game_id: str
    start_utc: datetime
    away: Team
    home: Team
    state: str  # 'pre' | 'in' | 'post' | 'off' (postponed / cancelled / suspended)

    @property
    def key(self) -> str:
        return f"{self.sport}-{self.game_id}"


def _get_json(url: str, timeout: int = 30):
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "dk-splits-robot"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_mlb(payload) -> list[OfficialGame]:
    out = []
    for d in payload.get("dates", []):
        for g in d.get("games", []):
            st = g.get("status", {})
            detailed = (st.get("detailedState") or "").lower()
            abstract = (st.get("abstractGameState") or "").lower()
            if any(w in detailed for w in ("postponed", "cancelled", "suspended")):
                state = "off"
            elif abstract == "preview":
                state = "pre"
            elif abstract == "live":
                state = "in"
            else:
                state = "post"

            def team(node) -> Team:
                t = node.get("team", {})
                raw = (t.get("teamName"), t.get("name"), t.get("clubName"), t.get("shortName"), t.get("abbreviation"))
                return Team(t.get("teamName") or t.get("name") or "", tuple(x for x in raw if x), t.get("abbreviation", ""))

            out.append(
                OfficialGame(
                    "MLB",
                    str(g["gamePk"]),
                    _iso(g["gameDate"]),
                    team(g["teams"]["away"]),
                    team(g["teams"]["home"]),
                    state,
                )
            )
    return out


def parse_espn(sport: str, payload) -> list[OfficialGame]:
    out = []
    for ev in payload.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        stype = comp.get("status", {}).get("type", {}) or ev.get("status", {}).get("type", {})
        name = (stype.get("name") or "").upper()
        if any(w in name for w in ("POSTPONED", "CANCELED", "CANCELLED", "SUSPENDED")):
            state = "off"
        else:
            state = stype.get("state") or "pre"
        sides = {}
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            disp, nick = t.get("displayName") or "", t.get("name") or ""
            school = disp[: -len(nick)].strip() if nick and disp.endswith(nick) else ""
            raw = (t.get("location"), t.get("shortDisplayName"), disp, school, t.get("abbreviation"), nick)
            sides[c.get("homeAway")] = Team(nick, tuple(x for x in raw if x), t.get("abbreviation", ""))
        if "home" not in sides or "away" not in sides:
            continue
        out.append(OfficialGame(sport, str(ev["id"]), _iso(ev["date"]), sides["away"], sides["home"], state))
    return out


def games_for(sport: str, day: date, get=_get_json) -> list[OfficialGame]:
    if sport == "MLB":
        return parse_mlb(get(MLB_SCHEDULE.format(d=day.isoformat())))
    path = ESPN_PATHS[sport].format(d=day.strftime("%Y%m%d"))
    last_err: Exception | None = None
    for host in ESPN_HOSTS:
        try:
            return parse_espn(sport, get(host + path))
        except (urllib.error.URLError, ValueError, OSError) as e:
            last_err = e
    raise RuntimeError(f"ESPN {sport} schedule unavailable: {last_err}")
