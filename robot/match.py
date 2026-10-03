"""Match a DraftKings game to its official game (and so to the app's card).

Rules -- a wrong match is worse than no match, so anything unsure is left out:
  - Pro leagues match on the team NICKNAME ("CHI White Sox" ends with "White Sox";
    MLB's "CWS" / "AZ" / "ATH" never have to line up with DK's "CHI" / "ARI").
  - CFB matches on the school name against every name ESPN gives, plus an alias
    list for the few DK spellings ESPN doesn't use ("Miami FL" -> "Miami").
  - Both teams must match. One team is enough ONLY when the start times agree to
    within 20 minutes and that is the sole candidate (a naming gap, not a guess).
  - Doubleheaders: the closest start time wins.
  - Numbers are assigned by TEAM, never by row position, and a neutral-site game
    DK lists the other way round is flipped back to the official home/away.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from .dk import DkGame, Side, when_to_utc
from .official import OfficialGame, Team

# DK label (normalized) -> ESPN name (normalized). Seeded from the 9/26 slate
# (100 DK labels, 2 misses) plus spellings known to differ between the two.
CFB_ALIASES = {
    "miami fl": "miami",
    "appalachian state": "app state",
    "ul lafayette": "louisiana",
    "louisiana lafayette": "louisiana",
    "southern mississippi": "southern miss",
    "central florida": "ucf",
    "connecticut": "uconn",
    "nevada las vegas": "unlv",
    "texas san antonio": "utsa",
    "north carolina state": "nc state",
    "mississippi": "ole miss",
    "florida international": "fiu",
    "sam houston state": "sam houston",
    # The Odds API (FanDuel) spellings, 10/3 slate: 54 of 54 matched, these two on time only
    "umass minutemen": "massachusetts minutemen",
    "mcneese state cowboys": "mcneese cowboys",
}
ONE_SIDE_MAX = timedelta(minutes=20)
BOTH_SIDES_MAX = timedelta(hours=4)


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    s = s.replace("&", " and ").replace("'", "").replace("’", "").replace(".", "")
    s = re.sub(r"[()\-/]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def team_matches(sport: str, dk_label: str, team: Team) -> bool:
    lab = norm(dk_label)
    if sport == "CFB":
        lab = CFB_ALIASES.get(lab, lab)
        return lab in {norm(n) for n in team.names}
    nick = norm(team.nickname)
    return bool(nick) and (lab == nick or lab.endswith(" " + nick))


@dataclass
class Match:
    game: OfficialGame
    flipped: bool  # DK lists this game the other way round (neutral site)
    note: str  # 'both' | 'one-side'

    def dk_label(self, dk: DkGame, side: str) -> str:
        """DK's label for the OFFICIAL home/away team."""
        if side == "home":
            return dk.away if self.flipped else dk.home
        return dk.home if self.flipped else dk.away


def match_game(sport: str, dk: DkGame, games: list[OfficialGame], now_utc: datetime) -> Match | None:
    return match_teams(sport, dk.away, dk.home, when_to_utc(dk.when_et, now_utc), games)


def match_teams(
    sport: str, away: str, home: str, when_utc: datetime | None, games: list[OfficialGame]
) -> Match | None:
    """Any source's 'AWAY @ HOME' + start time -> the official game (DK, FanDuel, ...)."""
    dk_time = when_utc
    both: list[tuple[timedelta, Match]] = []
    one: list[tuple[timedelta, Match]] = []
    for g in games:
        if g.sport != sport:
            continue
        dt = abs(dk_time - g.start_utc) if dk_time else timedelta(0)
        straight = (team_matches(sport, home, g.home), team_matches(sport, away, g.away))
        flipped = (team_matches(sport, home, g.away), team_matches(sport, away, g.home))
        if all(straight):
            both.append((dt, Match(g, False, "both")))
        elif all(flipped):
            both.append((dt, Match(g, True, "both")))
        elif any(straight):
            one.append((dt, Match(g, False, "one-side")))
        elif any(flipped):
            one.append((dt, Match(g, True, "one-side")))
    both = [x for x in both if x[0] <= BOTH_SIDES_MAX]
    if both:
        both.sort(key=lambda x: x[0])
        if len(both) > 1 and both[0][0] == both[1][0]:
            return None  # two equally good candidates -> refuse to guess
        return both[0][1]
    one = [x for x in one if dk_time and x[0] <= ONE_SIDE_MAX]
    if len(one) == 1:
        return one[0][1]
    return None


def _num(s: str) -> float | None:
    s = s.strip().replace("−", "-").replace("+", "")
    if s.lower() in ("pk", "pick", "ev", "even"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


@dataclass
class Split:
    handle_home: int
    bets_home: int
    handle_away: int
    bets_away: int
    line_home: float | None = None  # spread only (home team's number)


@dataclass
class TotalSplit:
    line: float | None
    handle_over: int
    bets_over: int
    handle_under: int
    bets_under: int


def team_split(rows: list[Side], home_label: str, away_label: str, with_line: bool) -> Split | None:
    def find(lab: str) -> tuple[Side, float | None] | None:
        for r in rows:
            if r.label == lab:
                return r, None
            if with_line and r.label.startswith(lab + " "):
                return r, _num(r.label[len(lab) + 1 :])
        return None

    h, a = find(home_label), find(away_label)
    if not h or not a or h[0] is a[0]:
        return None
    return Split(h[0].handle, h[0].bets, a[0].handle, a[0].bets, h[1] if with_line else None)


def total_split(rows: list[Side]) -> TotalSplit | None:
    over = next((r for r in rows if r.label.lower().startswith("over")), None)
    under = next((r for r in rows if r.label.lower().startswith("under")), None)
    if not over or not under:
        return None
    return TotalSplit(_num(over.label.split(" ", 1)[1]) if " " in over.label else None,
                      over.handle, over.bets, under.handle, under.bets)
