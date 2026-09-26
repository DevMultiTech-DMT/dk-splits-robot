"""One robot run.

1. SWEEP: every league with a game still to start today -> grab + save.
2. PRE-GAME: for each game starting within the next 40 minutes, wait for the
   15- and 10-minute marks and grab that league again (saving only those games).
   The waiting happens inside the run because GitHub's own timer can fire
   10-15 minutes late.

A "slate day" runs 7am -> 7am Eastern, so a game that starts after midnight
still belongs to the evening it was bet on, and the first grab after 7am is the
day's MORNING number.

  python -m robot.main --dry-run --no-wait   # look only: print, save nothing
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from . import dk, official, store
from .match import match_game, team_split, total_split

HORIZON = timedelta(minutes=40)
LEADS = (15, 10)
SLATE_ROLLOVER_HOUR_ET = 7


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def slate_day(now: datetime):
    return (now.astimezone(dk.ET) - timedelta(hours=SLATE_ROLLOVER_HOUR_ET)).date()


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


def run(dry_run: bool, wait: bool, sports: list[str], log=print, sleep=time.sleep, clock=utcnow) -> int:
    now = clock()
    day = slate_day(now)
    log(f"Robot run {store.iso(now)} | slate {day} | {'DRY RUN (saves nothing)' if dry_run else 'LIVE'}")
    games: dict[str, list[official.OfficialGame]] = {}
    for s in sports:
        try:
            games[s] = official.games_for(s, day)
        except Exception as e:  # one league's schedule being down never stops the others
            log(f"  {s}: official schedule unavailable ({e}) - skipped")
    active = [s for s in sports if any(eligible(g, now) for g in games.get(s, []))]
    log(f"  leagues with games still to start: {', '.join(active) or 'none'}")

    fs = None if dry_run else store.firestore_from_env()
    if not dry_run:
        log(f"  Firebase: {'ON -> collection splits' if fs else 'off (no key added yet)'}")
    history = store.load_rows(day.isoformat())
    all_rows: list[dict] = []

    def save(rows: list[dict], label: str) -> None:
        all_rows.extend(rows)
        if dry_run or not rows:
            return
        docs = [d for d in (store.build_doc(r, history) for r in rows if r.get("key")) if d]
        store.append_rows(day.isoformat(), rows)
        history.extend(rows)
        if fs:
            fs.write(docs)
        et = clock().astimezone(dk.ET).strftime("%H:%M ET")
        store.commit_data(f"data: {label} {et} ({len(docs)} games)")

    save(grab(active, games, now, None, log), "sweep")

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

    summarize(all_rows, log)
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DraftKings betting-splits robot")
    ap.add_argument("--dry-run", action="store_true", help="look only: print, save nothing")
    ap.add_argument("--no-wait", action="store_true", help="skip the pre-game waiting")
    ap.add_argument("--sports", default=",".join(official.SPORTS))
    a = ap.parse_args(argv)
    sports = [s.strip().upper() for s in a.sports.split(",") if s.strip()]
    return run(a.dry_run, not a.no_wait, sports)


if __name__ == "__main__":
    sys.exit(main())
