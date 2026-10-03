"""One robot run (every 30 minutes, around the clock).

0. CLEAN: delete every `splits` doc from an earlier day (current day only).
1. SWEEP: every league with a game still to start today -> grab + save.
2. PRE-GAME: for each game starting within the next 40 minutes, wait for the
   15- and 10-minute marks and grab that league again (saving only those games).
   The waiting happens inside the run because GitHub's own timer can fire
   10-15 minutes late.

The day is the Eastern calendar day: it starts at midnight, and the first grab
after midnight is the day's MORNING number (user's call 9/26).

  python -m robot.main --dry-run --no-wait   # look only: print, save nothing
  python -m robot.main --check-firebase      # prove the key works; touches no game data
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import dk, fanduel, official, store
from .match import match_game, match_teams, team_split, total_split

HORIZON = timedelta(minutes=40)
LEADS = (15, 10)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slate_day(now: datetime):
    """The Eastern calendar day — it starts at midnight."""
    return now.astimezone(dk.ET).date()


def eligible(g: official.OfficialGame, now: datetime) -> bool:
    """Still to start. Numbers are never recorded at or after the start."""
    return g.state not in ("post", "off") and g.start_utc > now


def build_row(sport: str, g: dk.DkGame, m, now: datetime, phase: str) -> dict:
    og = m.game
    home_lab, away_lab = m.dk_label(g, "home"), m.dk_label(g, "away")
    ml = team_split(g.markets.get("moneyline", []), home_lab, away_lab, with_line=False)
    sp = team_split(g.markets.get("spread", []), home_lab, away_lab, with_line=True)
    tot = total_split(g.markets.get("total", []))
    return {
        "grabbed_at_utc": store.iso(now),
        "sport": sport,
        "game_id": og.game_id,
        "key": og.key,
        "dk_event_id": g.event_id,
        "start_utc": store.iso(og.start_utc),
        "phase": phase,
        "min_to_start": int((og.start_utc - now).total_seconds() // 60),
        "match": m.note + ("-flipped" if m.flipped else ""),
        "away_dk": away_lab,
        "home_dk": home_lab,
        "ml_home_money": ml.handle_home if ml else None,
        "ml_home_bets": ml.bets_home if ml else None,
        "ml_away_money": ml.handle_away if ml else None,
        "ml_away_bets": ml.bets_away if ml else None,
        "sp_home_line": sp.line_home if sp else None,
        "sp_home_money": sp.handle_home if sp else None,
        "sp_home_bets": sp.bets_home if sp else None,
        "sp_away_money": sp.handle_away if sp else None,
        "sp_away_bets": sp.bets_away if sp else None,
        "tot_line": tot.line if tot else None,
        "over_money": tot.handle_over if tot else None,
        "over_bets": tot.bets_over if tot else None,
        "under_money": tot.handle_under if tot else None,
        "under_bets": tot.bets_under if tot else None,
    }


def unmatched_row(sport: str, g: dk.DkGame, now: datetime) -> dict:
    return {
        "grabbed_at_utc": store.iso(now), "sport": sport, "dk_event_id": g.event_id,
        "phase": "sweep", "match": f"UNMATCHED ({g.when_et})", "away_dk": g.away, "home_dk": g.home,
    }


def grab(sports, games, now, only: dict[str, str] | None, log, fetch_league=None):
    """-> rows. `only` = {game key: phase} for a pre-game grab; None = full sweep."""
    fetch_league = fetch_league or dk.fetch_league
    rows = []
    today_et = now.astimezone(dk.ET).date()
    for sport in sports:
        # a game that starts after midnight is listed under DK's NEXT date
        edates = ["today"]
        if any(eligible(g, now) and g.start_utc.astimezone(dk.ET).date() > today_et for g in games.get(sport, [])):
            edates.append("tomorrow")
        try:
            listed, seen = [], set()
            for ed in edates:
                for g in fetch_league(sport, ed):
                    if g.event_id not in seen:
                        seen.add(g.event_id)
                        listed.append(g)
        except dk.Blocked as e:
            log(f"  {sport}: DraftKings page FAILED ({e}) - nothing saved for {sport} this time")
            continue
        n_match = n_skip = 0
        for g in listed:
            m = match_game(sport, g, games.get(sport, []), now)
            if not m:
                if only is None:
                    rows.append(unmatched_row(sport, g, now))
                    log(f"  {sport}: could not match '{g.away} @ {g.home}' ({g.when_et}) - left blank")
                continue
            if only is not None and m.game.key not in only:
                continue
            if not eligible(m.game, now):
                n_skip += 1
                continue
            rows.append(build_row(sport, g, m, now, only[m.game.key] if only else "sweep"))
            n_match += 1
        log(f"  {sport}: DK listed {len(listed)}, saved {n_match}, skipped {n_skip} already started")
    return rows


def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class FanDuelPuller:
    """FanDuel moneylines for one run: budget, pull, match, save. OFF without a key.

    Budget state (today's credits used, last pull per sport, today's cap) lives in the
    `_robot_state` doc, which carries `day` and so is cleared at midnight like the rest."""

    def __init__(self, key, fs, games, day_s, log, now, fetch=None, credits_fn=None):
        self.key, self.fs, self.games, self.day_s, self.log = key, fs, games, day_s, log
        self.fetch = fetch or fanduel.fetch
        self.lines: list[dict] = []
        self.credits: int | None = None
        self.state: dict = {}
        self.on = bool(key)
        if not self.on:
            return
        try:
            self.credits = (credits_fn or fanduel.credits_left)(key)
        except Exception as e:
            log(f"  FanDuel: key check FAILED ({e}) - no odds this run")
            self.on = False
            return
        st = fs.get_state() if fs else {}
        credits = self.credits or 0
        if st.get("day") != day_s:
            st = {"day": day_s, "oddsUsed": 0, "lastPull": {}}
            st.update(oddsCap=fanduel.day_cap(credits, now), creditsAtCap=credits)
        elif credits > st.get("creditsAtCap", credits):
            # the monthly reset landed mid-day (1st, 12 AM UTC = 8 PM ET): start the new budget now
            st.update(oddsUsed=0, oddsCap=fanduel.day_cap(credits, now), creditsAtCap=credits)
            log(f"  FanDuel: credits reset detected ({credits}) - new month's budget starts now")
        self.state = st
        log(f"  FanDuel: ON | {self.credits} credits left | today's cap {st['oddsCap']}, used {st['oddsUsed']}")

    def kind_for_sweep(self, sport: str, now: datetime) -> str | None:
        """'morning' while any of today's games still lacks a morning line, else 'refresh'."""
        keys = [g.key for g in self.games.get(sport, []) if eligible(g, now)]
        if not keys:
            return None
        if not self.fs:
            return "morning"
        ex = self.fs.existing(keys)
        return "morning" if any(not ex.get(k, {}).get("fdMorningAt") for k in keys) else "refresh"

    def pull(self, sport: str, kind: str, now: datetime) -> None:
        if not self.on:
            return
        st = self.state
        last = st.setdefault("lastPull", {}).get(sport)
        gap = now - _parse_iso(last) if last else None
        if gap is not None and (gap < fanduel.MIN_GAP or (kind == "refresh" and gap < fanduel.REFRESH_EVERY)):
            return
        if not fanduel.allowed(kind, st.get("oddsUsed", 0), st.get("oddsCap", 0), self.credits):
            self.log(f"  FanDuel {sport}: {kind} pull skipped (budget: used {st.get('oddsUsed', 0)} of "
                     f"{st.get('oddsCap', 0)} today, {self.credits} left)")
            return
        elig = [g for g in self.games.get(sport, []) if eligible(g, now)]
        if not elig:
            return
        to = max(g.start_utc for g in elig) + timedelta(minutes=30)
        try:
            events, rem = self.fetch(sport, self.key, now, to)
        except Exception as e:
            self.log(f"  FanDuel {sport}: pull FAILED ({e})")
            return
        cost = (self.credits - rem) if (rem is not None and self.credits is not None) else (1 if events else 0)
        if rem is not None:
            self.credits = rem
        st["oddsUsed"] = st.get("oddsUsed", 0) + max(cost, 0)
        st["lastPull"][sport] = store.iso(now)
        matched = []
        for e in events:
            m = match_teams(sport, e.away, e.home, e.start_utc, elig)
            if not m or not eligible(m.game, now):
                continue
            # prices follow the TEAM: a neutral-site game listed the other way round is flipped back
            ml_away, ml_home = (e.ml_home, e.ml_away) if m.flipped else (e.ml_away, e.ml_home)
            matched.append((m.game, ml_away, ml_home, e.book_at))
        if self.fs:
            ex = self.fs.existing([g.key for g, *_ in matched])
            docs = [
                store.build_fd_doc(
                    key=g.key, sport=sport, game_id=g.game_id, start_utc=store.iso(g.start_utc),
                    ml_away=a, ml_home=h, grabbed_at=store.iso(now), book_at=b,
                    existing=ex.get(g.key), day=self.day_s,
                )
                for g, a, h, b in matched
            ]
            self.fs.write(docs)
            self.fs.set_state(st)
        for g, a, h, _ in matched:
            self.lines.append({"sport": sport, "game": f"{g.away.abbr} @ {g.home.abbr}", "start": g.start_utc,
                               "kind": kind, "away": a, "home": h})
        self.log(f"  FanDuel {sport} ({kind}): {len(matched)} game(s) matched of {len(events)}, "
                 f"cost {cost}, {self.credits} credits left")


def run(dry_run: bool, wait: bool, sports: list[str], log=print, sleep=time.sleep, clock=utcnow,
        with_odds: bool = False) -> int:
    now = clock()
    day = slate_day(now)
    log(f"Robot run {store.iso(now)} | slate {day} | {'DRY RUN (saves nothing)' if dry_run else 'LIVE'}")
    # automatic stop (user 10/3: "just for today"): after the ROBOT_UNTIL day, pull nothing
    until = os.environ.get("ROBOT_UNTIL", "").strip()
    if until and not dry_run and day.isoformat() > until:
        log(f"  STOPPED: today ({day}) is past ROBOT_UNTIL ({until}) - nothing pulled, no credits spent")
        return 0
    games: dict[str, list[official.OfficialGame]] = {}
    for s in sports:
        try:
            games[s] = official.games_for(s, day)
        except Exception as e:  # one league's schedule being down never stops the others
            log(f"  {s}: official schedule unavailable ({e}) - skipped")
    active = [s for s in sports if any(eligible(g, now) for g in games.get(s, []))]
    log(f"  leagues with games still to start: {', '.join(active) or 'none'}")

    day_s = day.isoformat()
    fs = None if dry_run else store.firestore_from_env()
    if not dry_run:
        log(f"  Firebase: {'ON -> collection splits' if fs else 'OFF (no key) - nothing will be saved'}")
    if fs:
        gone = fs.delete_before(day_s)
        if gone:
            log(f"  cleared {gone} doc(s) from before {day_s} (current day only)")
    all_rows: list[dict] = []

    def save(rows: list[dict], label: str) -> None:
        all_rows.extend(rows)
        if not fs or not rows:
            return
        keyed = [r for r in rows if r.get("key")]
        existing = fs.existing(sorted({r["key"] for r in keyed}))
        docs = [d for d in (store.build_doc(r, existing.get(r["key"]), day_s) for r in keyed) if d]
        n = fs.write(docs)
        log(f"  saved {n} game(s) to Firebase ({label})")

    save(grab(active, games, now, None, log), "sweep")

    # FanDuel moneylines: only with the ODDS_API_KEY secret; a look-only run spends
    # credits only when asked (--with-odds)
    odds_key = os.environ.get("ODDS_API_KEY", "").strip() if (with_odds or not dry_run) else ""
    if not odds_key and not dry_run:
        log("  FanDuel: OFF (no ODDS_API_KEY)")
    fd = FanDuelPuller(odds_key, fs, games, day_s, log, now)
    for s in active:
        kind = fd.kind_for_sweep(s, now)
        if kind:
            fd.pull(s, kind, now)

    if wait:
        targets: dict[datetime, dict[str, str]] = defaultdict(dict)
        sport_of: dict[str, str] = {}
        for s in active:
            for g in games[s]:
                if not eligible(g, now):
                    continue
                for lead in LEADS:
                    t = (g.start_utc - timedelta(minutes=lead)).replace(second=0, microsecond=0)
                    if now < t <= now + HORIZON:
                        targets[t][g.key] = f"T-{lead}"
                        sport_of[g.key] = s
        for t in sorted(targets):
            pause = (t - clock()).total_seconds()
            if pause > 0:
                log(f"  waiting until {t.astimezone(dk.ET):%I:%M %p ET} for {len(targets[t])} pre-game grab(s)")
                sleep(pause)
            keys = targets[t]
            lg = sorted({sport_of[k] for k in keys})
            save(grab(lg, games, clock(), keys, log), "pre-game")
            for s in lg:
                phases = {p for k, p in keys.items() if sport_of[k] == s}
                fd.pull(s, "T-10" if "T-10" in phases else "T-15", clock())

    summarize(all_rows, log)
    summarize_fanduel(fd.lines, log)
    return 0


def summarize(rows: list[dict], log) -> None:
    lines = ["", "| Sport | Game (away @ home) | Start (ET) | When | Moneyline: home money / bets | Spread: home money / bets |",
             "|---|---|---|---|---|---|"]
    for r in rows:
        if not r.get("key"):
            lines.append(f"| {r['sport']} | {r['away_dk']} @ {r['home_dk']} | - | {r['match']} | - | - |")
            continue
        start = datetime.fromisoformat(r["start_utc"].replace("Z", "+00:00")).astimezone(dk.ET)
        ml = f"{r['ml_home_money']}% / {r['ml_home_bets']}%" if r["ml_home_bets"] is not None else "-"
        sp = f"{r['sp_home_money']}% / {r['sp_home_bets']}% ({r['sp_home_line']:+g})" if r["sp_home_bets"] is not None and r["sp_home_line"] is not None else "-"
        lines.append(f"| {r['sport']} | {r['away_dk']} @ {r['home_dk']} | {start:%m/%d %I:%M %p} | {r['phase']} | {ml} | {sp} |")
    text = "\n".join(lines)
    log(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def summarize_fanduel(lines: list[dict], log) -> None:
    if not lines:
        return
    out = ["", "| Sport | Game (away @ home) | Start (ET) | When | FanDuel away | FanDuel home |", "|---|---|---|---|---|---|"]
    fmt = lambda n: f"+{n}" if n > 0 else str(n)  # noqa: E731
    for r in lines:
        out.append(f"| {r['sport']} | {r['game']} | {r['start'].astimezone(dk.ET):%m/%d %I:%M %p} | {r['kind']} "
                   f"| {fmt(r['away'])} | {fmt(r['home'])} |")
    text = "\n".join(out)
    log(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DraftKings betting-splits robot")
    ap.add_argument("--dry-run", action="store_true", help="look only: print, save nothing")
    ap.add_argument("--no-wait", action="store_true", help="skip the pre-game waiting")
    ap.add_argument("--check-firebase", action="store_true",
                    help="prove the key works: write, read back and delete one throwaway doc")
    ap.add_argument("--check-odds", action="store_true",
                    help="prove the Odds API key works and show credits left (free: spends nothing)")
    ap.add_argument("--with-odds", action="store_true",
                    help="look-only runs also pull FanDuel (spends ~1 credit per sport)")
    ap.add_argument("--sports", default=",".join(official.SPORTS))
    a = ap.parse_args(argv)
    if a.check_odds:
        key = os.environ.get("ODDS_API_KEY", "").strip()
        if not key:
            print("Odds API check: NO KEY (ODDS_API_KEY secret missing)")
            return 1
        try:
            left = fanduel.credits_left(key)
        except Exception as e:
            print(f"Odds API check: FAILED ({e})")
            return 1
        now = utcnow()
        print(f"Odds API check: OK | {left} credits left | {fanduel.days_left(now)} day(s) to the reset on "
              f"{fanduel.next_reset(now):%b %d} 12 AM UTC | today's budget: {fanduel.day_cap(left or 0, now)}")
        return 0
    if a.check_firebase:
        fs = store.firestore_from_env()
        if not fs:
            print("Firebase check: NO KEY (FIREBASE_SERVICE_ACCOUNT secret missing)")
            return 1
        result = fs.self_test()
        print(f"Firebase check (project {fs.project}, collection {store.COLLECTION}): {result}")
        return 0 if result == "OK" else 1
    sports = [s.strip().upper() for s in a.sports.split(",") if s.strip()]
    return run(a.dry_run, not a.no_wait, sports, with_odds=a.with_odds)


if __name__ == "__main__":
    sys.exit(main())
