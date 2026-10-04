"""The robot: TWO kinds of grabs, nothing else (user 10/3).

  1. MORNING, right after midnight Eastern: clear yesterday's docs, then grab every game
     of the new day (DraftKings splits + FanDuel lines, incl. MLB/NHL run/puck lines).
  2. TEN MINUTES BEFORE each game: grab that game's numbers again (same sources).

Between grabs the run sleeps. GitHub's own timer proved best-effort (on 10/3 it skipped
every slot for an hour), so the robot wakes ITSELF at midnight and at each 10-minute
mark; GitHub ends a job at 6 hours, so before that the run starts a fresh one that
carries on with the same plan (kept in Firestore `splits/_robot_plan`). GitHub's hourly
timer is only a backup that restarts the robot if it ever stops -- a restart grabs
nothing that's already been grabbed.

  python -m robot.main --dry-run --no-wait   # look only: print, save nothing
  python -m robot.main --check-firebase      # prove the key works; touches no game data
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import dk, fanduel, official, store
from .match import match_game, match_teams, team_split, total_split

LEAD = timedelta(minutes=10)  # the one pre-game grab ("only ... ten minutes before")
CLUSTER = timedelta(minutes=6)  # same-sport starts this close share one grab
JOB_MINUTES = 325  # GitHub ends a job at 6 hours: hand off to a fresh run before that
AFTER_MIDNIGHT = timedelta(minutes=2)  # the morning grab, just after the day turns
# the app's PULL-DOWN (user 10/3: "when I pull down to refresh, everything gets pulled exactly
# when I do that"): a sleeping robot looks for it every POLL seconds and grabs every game still
# to start (splits + FanDuel); a request older than REFRESH_STALE was given up on by the app
POLL = 15
REFRESH_STALE = timedelta(minutes=10)


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
        "phase": "morning", "match": f"UNMATCHED ({g.when_et})", "away_dk": g.away, "home_dk": g.home,
    }


def grab(sports, games, now, only: dict[str, str] | None, log, fetch_league=None, retry_wait: float = 5.0):
    """-> rows. `only` = {game key: phase} for a pre-game grab; None = every game (morning)."""
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
            # 10/3 06:10 UTC: DK once answered MLB + CFB with an EMPTY list one minute after
            # listing them all (NHL fine, same run) -- an empty answer while the official
            # schedule still has games to start gets ONE retry before it's believed
            for attempt in (1, 2):
                for ed in edates:
                    for g in fetch_league(sport, ed):
                        if g.event_id not in seen:
                            seen.add(g.event_id)
                            listed.append(g)
                if listed or attempt == 2 or not any(eligible(g, now) for g in games.get(sport, [])):
                    break
                log(f"  {sport}: DraftKings listed nothing although games are still to start - retrying once")
                time.sleep(retry_wait)
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
            rows.append(build_row(sport, g, m, now, only[m.game.key] if only else "morning"))
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
        # one-day raise (user 10/3: every game's 10-minute line today): ODDS_CAP_TODAY="YYYY-MM-DD=N"
        raise_day, _, raise_n = os.environ.get("ODDS_CAP_TODAY", "").strip().partition("=")
        if raise_day.strip() == day_s and raise_n.strip().isdigit():
            st["oddsCap"] = max(st.get("oddsCap", 0), int(raise_n))
        self.state = st
        log(f"  FanDuel: ON | {self.credits} credits left | today's cap {st['oddsCap']}, used {st['oddsUsed']}")

    def pull(self, sport: str, kind: str, now: datetime, only_keys: set[str] | None = None) -> None:
        """kind 'morning' (every game of the sport), 'T-10' (only `only_keys`, the games
        about to start -- the other games keep their own lines until their own T-10), or
        'refresh' (the app's pull-down: every game of the sport still to start)."""
        if not self.on:
            return
        st = self.state
        refresh = kind == "refresh"
        last = st.setdefault("lastPull", {}).get(sport)
        last_refresh = st.setdefault("lastRefresh", {}).get(sport)
        if refresh:
            newest = max((_parse_iso(x) for x in (last, last_refresh) if x), default=None)
            if newest and now - newest < fanduel.REFRESH_GAP:
                return
        elif last and now - _parse_iso(last) < fanduel.MIN_GAP:
            return
        if not fanduel.allowed(kind, st.get("oddsUsed", 0), st.get("oddsCap", 0), self.credits):
            self.log(f"  FanDuel {sport}: {kind} pull skipped (budget: used {st.get('oddsUsed', 0)} of "
                     f"{st.get('oddsCap', 0)} today, {self.credits} left)")
            return
        elig = [g for g in self.games.get(sport, []) if eligible(g, now) and (only_keys is None or g.key in only_keys)]
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
        if refresh:  # counted apart: a pull-down never uses up the day's 10-minute grabs
            st["refreshUsed"] = st.get("refreshUsed", 0) + max(cost, 0)
            st["lastRefresh"][sport] = store.iso(now)
        else:
            st["oddsUsed"] = st.get("oddsUsed", 0) + max(cost, 0)
            st["lastPull"][sport] = store.iso(now)
        matched = []
        for e in events:
            m = match_teams(sport, e.away, e.home, e.start_utc, elig)
            if not m or not eligible(m.game, now):
                continue
            # prices follow the TEAM: a neutral-site game listed the other way round is flipped back
            ml_away, ml_home = (e.ml_home, e.ml_away) if m.flipped else (e.ml_away, e.ml_home)
            sp_away, sp_home = (e.sp_home, e.sp_away) if m.flipped else (e.sp_away, e.sp_home)
            matched.append((m.game, ml_away, ml_home, e.book_at, sp_away, sp_home))
        if self.fs:
            ex = self.fs.existing([g.key for g, *_ in matched])
            docs = [
                store.build_fd_doc(
                    key=g.key, sport=sport, game_id=g.game_id, start_utc=store.iso(g.start_utc),
                    ml_away=a, ml_home=h, grabbed_at=store.iso(now), book_at=b,
                    existing=ex.get(g.key), day=self.day_s, sp_away=spa, sp_home=sph,
                )
                for g, a, h, b, spa, sph in matched
            ]
            self.fs.write(docs)
            self.fs.set_state(st)
        for g, a, h, *_ in matched:
            self.lines.append({"sport": sport, "game": f"{g.away.abbr} @ {g.home.abbr}", "start": g.start_utc,
                               "kind": kind, "away": a, "home": h})
        self.log(f"  FanDuel {sport} ({kind}): {len(matched)} game(s) matched of {len(events)}, "
                 f"cost {cost}, {self.credits} credits left")


@dataclass
class Target:
    """One 10-minute grab: a sport + the games about to start (same-sport starts within
    CLUSTER minutes share it, taken no later than 3 minutes before the earliest of them)."""

    when: datetime
    sport: str
    keys: set
    tid: str


def plan_targets(games: dict, now: datetime, done: set) -> list[Target]:
    out = []
    for sport, gs in games.items():
        items = sorted(
            (((g.start_utc - LEAD).replace(second=0, microsecond=0), g) for g in gs if eligible(g, now)),
            key=lambda x: x[0],
        )
        i = 0
        while i < len(items):
            t0 = items[i][0]
            cluster = [items[i]]
            i += 1
            while i < len(items) and items[i][0] <= t0 + CLUSTER:
                cluster.append(items[i])
                i += 1
            when = min(max(t for t, _ in cluster), min(g.start_utc for _, g in cluster) - timedelta(minutes=3))
            tid = f"{sport}|{store.iso(when)}"
            if tid not in done:
                out.append(Target(when, sport, {g.key for _, g in cluster}, tid))
    return sorted(out, key=lambda t: t.when)


def hears(fs) -> bool:
    """A store that can carry the app's pull-down (the live Firestore; not a bare test fake)."""
    return fs is not None and hasattr(fs, "get_refresh_request")


def pending_refresh(fs, now: datetime, handled: int) -> int | None:
    """The app's pull-down not answered yet (its requestedAt, ms), or None."""
    if not hears(fs):
        return None
    try:
        at = fs.get_refresh_request()
    except Exception:  # a failed look never stops the schedule
        return None
    if at is None or at <= handled or now.timestamp() * 1000 - at > REFRESH_STALE.total_seconds() * 1000:
        return None
    return at


def nap(seconds: float, fs, sleep, clock, handled: int) -> bool:
    """Sleep `seconds`; with a store that carries the pull-down, look for one every POLL
    seconds. True = the app asked for fresh numbers (stop sleeping and grab)."""
    if not hears(fs):
        sleep(seconds)
        return False
    end = clock() + timedelta(seconds=seconds)
    while True:
        left = (end - clock()).total_seconds()
        if left <= 0:
            return False
        sleep(min(POLL, left))
        if pending_refresh(fs, clock(), handled):
            return True


def next_midnight(now: datetime) -> datetime:
    """Just after the next midnight Eastern: the next day's morning grab."""
    et = now.astimezone(dk.ET)
    nxt = datetime.combine(et.date() + timedelta(days=1), datetime.min.time(), tzinfo=dk.ET)
    return nxt.astimezone(timezone.utc) + AFTER_MIDNIGHT


def dispatch_next(log) -> bool:
    """Start the next run (GitHub ends a job at 6 hours) with this run's own GITHUB_TOKEN."""
    token, repo = os.environ.get("GH_TOKEN", ""), os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repo:
        log("  hand-off: not running on GitHub - no next run started")
        return False
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/robot.yml/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"mode": "live"}}).encode(),
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status == 204
    except Exception as e:  # e.g. the workflow was switched off: the robot simply stops
        log(f"  hand-off refused ({e}) - robot stops; GitHub's hourly timer restarts it if it's on")
        return False
    log("  hand-off: next run started; it carries on with the same plan")
    return ok


def load_games(sports: list[str], day, log) -> dict:
    games: dict[str, list[official.OfficialGame]] = {}
    for s in sports:
        try:
            games[s] = official.games_for(s, day)
        except Exception as e:  # one league's schedule being down never stops the others
            log(f"  {s}: official schedule unavailable ({e}) - skipped")
    return games


def save_rows(fs, rows: list[dict], day_s: str, log, label: str) -> None:
    if not fs or not rows:
        return
    keyed = [r for r in rows if r.get("key")]
    existing = fs.existing(sorted({r["key"] for r in keyed}))
    docs = [d for d in (store.build_doc(r, existing.get(r["key"]), day_s) for r in keyed) if d]
    n = fs.write(docs)
    log(f"  saved {n} game(s) to Firebase ({label})")


def fanduel_for(fs, games, day_s, log, now, dry_run: bool, with_odds: bool) -> FanDuelPuller:
    """FanDuel only with the ODDS_API_KEY secret, and never while the app's Auto-fill
    switch is off (credits saved for the days it's on; DK splits keep coming, free)."""
    key = os.environ.get("ODDS_API_KEY", "").strip() if (with_odds or not dry_run) else ""
    if not key and not dry_run:
        log("  FanDuel: OFF (no ODDS_API_KEY)")
    if key and fs:
        try:
            switch = fs.app_auto_fill()
        except Exception:
            switch = None
        if switch is False:
            log("  FanDuel: PAUSED - Auto-fill is switched off in the app (no credits spent)")
            key = ""
    return FanDuelPuller(key, fs, games, day_s, log, now)


def run(dry_run: bool, wait: bool, sports: list[str], log=print, sleep=time.sleep, clock=utcnow,
        with_odds: bool = False, stop_at: datetime | None = None, dispatch=None,
        job_minutes: int = JOB_MINUTES) -> int:
    started = clock()
    deadline = started + timedelta(minutes=job_minutes)
    dispatch = dispatch or dispatch_next
    fs = None if dry_run else store.firestore_from_env()
    log(f"Robot run {store.iso(started)} | {'DRY RUN (saves nothing)' if dry_run else 'LIVE'} | "
        f"Firebase {'ON -> collection splits' if fs else 'OFF (nothing will be saved)'}")
    mem_plan: dict = {}
    handled = 0  # the last pull-down answered
    if hears(fs):
        try:
            handled = int(fs.get_refresh_done().get("handled") or 0)
        except Exception:
            handled = 0
    while True:
        now = clock()
        day = slate_day(now)
        day_s = day.isoformat()
        until = os.environ.get("ROBOT_UNTIL", "").strip()
        if until and not dry_run and day_s > until:
            log(f"  STOPPED: today ({day_s}) is past ROBOT_UNTIL ({until}) - nothing pulled, no credits spent")
            return 0
        games = load_games(sports, day, log)
        plan = fs.get_plan() if fs else dict(mem_plan)
        if plan.get("day") != day_s:
            plan = {"day": day_s, "morningDone": False, "done": []}
            # a day whose morning grab the earlier robot version already took (10/3 05:34 UTC)
            if fs:
                st = fs.get_state()
                plan["morningDone"] = st.get("day") == day_s and bool(st.get("lastPull"))
        fd = fanduel_for(fs, games, day_s, log, now, dry_run, with_odds)
        rows: list[dict] = []

        if not plan["morningDone"]:
            if fs:
                gone = fs.delete_before(day_s)
                if gone:
                    log(f"  cleared {gone} doc(s) from before {day_s} (current day only)")
            active = [s for s in sports if any(eligible(g, now) for g in games.get(s, []))]
            log(f"  MORNING grab for {day_s}: {', '.join(active) or 'no games'}")
            got = grab(active, games, now, None, log)
            save_rows(fs, got, day_s, log, "morning")
            rows += got
            for s in active:
                fd.pull(s, "morning", now)
            plan["morningDone"] = True

        # the 10-minute grabs due now (a late one still counts while its games haven't started)
        for t in plan_targets(games, now, set(plan["done"])):
            if t.when > now + timedelta(seconds=30):
                break
            log(f"  10-MINUTE grab: {t.sport}, {len(t.keys)} game(s)")
            got = grab([t.sport], games, now, {k: "T-10" for k in t.keys}, log)
            save_rows(fs, got, day_s, log, "10 minutes before")
            rows += got
            fd.pull(t.sport, "T-10", now, only_keys=t.keys)
            plan["done"].append(t.tid)

        # the app's pull-down: every game still to start, splits + FanDuel, right now
        req = pending_refresh(fs, now, handled)
        if req:
            active = [s for s in sports if any(eligible(g, now) for g in games.get(s, []))]
            keys = {g.key: "refresh" for s in active for g in games.get(s, []) if eligible(g, now)}
            log(f"  PULL-DOWN REFRESH (the app asked): {', '.join(active) or 'no games'}, {len(keys)} game(s)")
            got = grab(active, games, now, keys, log)
            save_rows(fs, got, day_s, log, "pull-down refresh")
            rows += got
            for s in active:
                fd.pull(s, "refresh", now)
            handled = req
            try:
                fs.set_refresh_done({"handled": req, "at": store.iso(clock()), "games": len(keys),
                                     "splitsSaved": len(got), "credits": fd.credits})
            except Exception as e:
                log(f"  pull-down answer not saved ({e}) - the app times out and shows what is saved")

        if fs:
            fs.set_plan(plan)
        else:
            mem_plan.clear()
            mem_plan.update(plan)
        summarize(rows, log)
        summarize_fanduel(fd.lines, log)
        if not wait:
            return 0

        upcoming = plan_targets(games, clock(), set(plan["done"]))
        nxt = upcoming[0] if upcoming else None
        when = nxt.when if nxt else next_midnight(clock())
        if stop_at is not None and when > stop_at:
            return 0
        if when > deadline:
            pause = (deadline - clock()).total_seconds()
            if pause > 0:
                log(f"  sleeping until {deadline.astimezone(dk.ET):%I:%M %p ET}, then handing off to a fresh run")
                if nap(pause, fs, sleep, clock, handled):
                    continue  # a pull-down came in: grab it, then back to sleep
            dispatch(log)
            return 0
        pause = (when - clock()).total_seconds()
        if pause > 0:
            what = f"10 minutes before {nxt.sport} ({len(nxt.keys)} game(s))" if nxt else "midnight: the next morning grab"
            log(f"  sleeping until {when.astimezone(dk.ET):%I:%M %p ET} - {what}")
            nap(pause, fs, sleep, clock, handled)  # a pull-down wakes it early; the loop grabs it


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
