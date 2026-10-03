"""Robot tests, built on REAL pages saved on 9/26 and 10/3 (tests/fixtures/).

  python -m unittest discover -s tests -t . -v
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robot import dk, fanduel, main, official, store
from robot.match import match_game, match_teams, team_split

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 9, 26, 16, 0, tzinfo=timezone.utc)  # 12:00 PM ET, before every fixture game


def page(name):
    return (FIX / name).read_text(encoding="utf-8")


def dk_games(*names):
    out = []
    for n in names:
        out += dk.parse_page(page(n))
    return out


def load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


MLB = official.parse_mlb(load("mlb_schedule.json"))
NFL = official.parse_espn("NFL", load("espn_nfl.json"))
WNBA = official.parse_espn("WNBA", load("espn_wnba.json"))
CFB = official.parse_espn("CFB", load("espn_cfb.json"))


class ParsePage(unittest.TestCase):
    def test_mlb_page(self):
        games = dk_games("dk_mlb_p1.html")
        self.assertEqual(len(games), 10)
        g = games[0]
        self.assertEqual((g.event_id, g.away, g.home, g.when_et), ("34726368", "PIT Pirates", "DET Tigers", "9/26, 01:10PM"))
        self.assertEqual(sorted(g.markets), ["moneyline", "spread", "total"])
        det = next(s for s in g.markets["moneyline"] if s.label == "DET Tigers")
        self.assertEqual((det.odds, det.handle, det.bets), ("-122", 51, 52))
        self.assertEqual([s.label for s in g.markets["total"]], ["Over 9", "Under 9"])

    def test_nfl_titles_lose_their_logo_images(self):
        g = dk_games("dk_nfl_p1.html")[0]
        self.assertEqual((g.away, g.home), ("LA Chargers", "BUF Bills"))

    def test_cfb_blowout_without_moneyline(self):
        fsu = next(g for g in dk_games(*[f"dk_cfb_p{i}.html" for i in range(1, 6)]) if g.home == "Florida State")
        self.assertNotIn("moneyline", fsu.markets)
        self.assertIn("spread", fsu.markets)

    def test_pagination_stops_when_dk_repeats_the_last_page(self):
        pages = {1: page("dk_mlb_p1.html"), 2: page("dk_mlb_p2.html")}
        calls = []

        def fake_fetch(eg, p, edate):
            calls.append(p)
            return pages.get(p, pages[2])  # DK repeats the last page past the end

        got = dk.fetch_league("MLB", "today", fetch=fake_fetch)
        self.assertEqual(len(got), 12)
        self.assertEqual(len({g.event_id for g in got}), 12)
        self.assertEqual(calls, [1, 2])  # page 2 had < 10 games -> stop

    def test_times_are_eastern(self):
        self.assertEqual(dk.when_to_utc("9/26, 01:10PM", NOW), datetime(2026, 9, 26, 17, 10, tzinfo=timezone.utc))
        self.assertEqual(dk.when_to_utc("9/26, 12:00AM", NOW), datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc))
        new_year = datetime(2026, 12, 31, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(dk.when_to_utc("1/2, 01:00PM", new_year).year, 2027)

    def test_blocked_page_is_reported_not_parsed(self):
        with self.assertRaises(dk.Blocked):
            orig = dk.urllib.request.urlopen

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b"<html><title>Access Denied</title></html>"

            dk.urllib.request.urlopen = lambda *a, **k: R()
            try:
                dk.fetch_page(84240, 1)
            finally:
                dk.urllib.request.urlopen = orig


class Matching(unittest.TestCase):
    def test_every_mlb_game_matches_with_the_app_game_id(self):
        games = dk_games("dk_mlb_p1.html", "dk_mlb_p2.html")
        keys = {}
        for g in games:
            m = match_game("MLB", g, MLB, NOW)
            self.assertIsNotNone(m, f"{g.away} @ {g.home}")
            self.assertFalse(m.flipped)
            keys[f"{g.away} @ {g.home}"] = m.game.key
        self.assertEqual(keys["PIT Pirates @ DET Tigers"], "MLB-824219")
        self.assertEqual(keys["COL Rockies @ CHI White Sox"], "MLB-824543")  # DK "CHI" vs MLB "CWS"
        self.assertEqual(keys["ARI Diamondbacks @ SD Padres"], "MLB-823245")  # DK "ARI" vs MLB "AZ"
        self.assertEqual(keys["HOU Astros @ Athletics"], "MLB-824949")  # no city prefix at all

    def test_nfl_and_wnba(self):
        nfl = dk_games("dk_nfl_p1.html", "dk_nfl_p2.html")
        matched = [g for g in nfl if match_game("NFL", g, NFL, NOW)]
        self.assertEqual(len(matched), 14)  # the other 2 are Monday/Thursday games (not in Sunday's schedule)
        for g in dk_games("dk_wnba_p1.html"):
            self.assertIsNotNone(match_game("WNBA", g, WNBA, NOW), g.home)

    def test_every_cfb_game_matches_including_aliases(self):
        games = dk_games(*[f"dk_cfb_p{i}.html" for i in range(1, 6)])
        self.assertEqual(len(games), 50)
        misses = [f"{g.away} @ {g.home}" for g in games if not match_game("CFB", g, CFB, NOW)]
        self.assertEqual(misses, [])
        miami = next(g for g in games if g.home == "Miami FL")
        self.assertEqual(match_game("CFB", miami, CFB, NOW).note, "both")

    def test_numbers_follow_the_team_not_the_row_order(self):
        g = dk_games("dk_mlb_p1.html")[0]  # DK lists DET (home) first
        m = match_game("MLB", g, MLB, NOW)
        s = team_split(g.markets["moneyline"], m.dk_label(g, "home"), m.dk_label(g, "away"), with_line=False)
        self.assertEqual((s.handle_home, s.bets_home, s.handle_away, s.bets_away), (51, 52, 49, 48))
        sp = team_split(g.markets["spread"], m.dk_label(g, "home"), m.dk_label(g, "away"), with_line=True)
        self.assertEqual(sp.line_home, 1.5)  # "DET Tigers +1.5"

    def test_neutral_site_listed_the_other_way_round_is_flipped_back(self):
        g = dk_games("dk_mlb_p1.html")[0]
        swapped = dk.DkGame(g.event_id, g.home, g.away, g.when_et, g.markets)  # DK says DET @ PIT
        m = match_game("MLB", swapped, MLB, NOW)
        self.assertTrue(m.flipped)
        row = main.build_row("MLB", swapped, m, NOW, "sweep")
        self.assertEqual((row["home_dk"], row["ml_home_money"], row["ml_home_bets"]), ("DET Tigers", 51, 52))

    def test_one_team_is_enough_only_with_the_same_start_time(self):
        g = dk_games("dk_mlb_p1.html")[0]
        typo = dk.DkGame(g.event_id, "PIT Buccos", g.home, g.when_et, g.markets)
        m = match_game("MLB", typo, MLB, NOW)
        self.assertEqual((m.game.key, m.note), ("MLB-824219", "one-side"))
        late = dk.DkGame(g.event_id, "PIT Buccos", g.home, "9/26, 04:10PM", g.markets)
        self.assertIsNone(match_game("MLB", late, MLB, NOW))

    def test_doubleheader_takes_the_closest_start(self):
        base = next(x for x in MLB if x.key == "MLB-824219")
        g2 = official.OfficialGame("MLB", "999", base.start_utc + timedelta(hours=5), base.away, base.home, "pre")
        g = dk_games("dk_mlb_p1.html")[0]
        night = dk.DkGame("1", g.away, g.home, "9/26, 06:10PM", g.markets)
        self.assertEqual(match_game("MLB", g, MLB + [g2], NOW).game.game_id, "824219")
        self.assertEqual(match_game("MLB", night, MLB + [g2], NOW).game.game_id, "999")

    def test_two_identical_candidates_are_refused(self):
        base = next(x for x in MLB if x.key == "MLB-824219")
        twin = official.OfficialGame("MLB", "999", base.start_utc, base.away, base.home, "pre")
        self.assertIsNone(match_game("MLB", dk_games("dk_mlb_p1.html")[0], [base, twin], NOW))


class FakeFirestore:
    """Stands in for store.Firestore: a dict of docs, same methods."""

    def __init__(self, docs=None, auto_fill=None):
        self.docs = dict(docs or {})
        self.writes = []
        self.auto_fill = auto_fill

    def app_auto_fill(self):
        return self.auto_fill

    def existing(self, keys):
        return {k: dict(self.docs[k]) for k in keys if k in self.docs}

    def write(self, docs):
        for d in docs:
            self.docs[d["key"]] = {**self.docs.get(d["key"], {}), **d}
            self.writes.append(dict(d))
        return len(docs)

    def delete_before(self, day):
        old = [k for k, d in self.docs.items() if d.get("day", "") < day]
        for k in old:
            del self.docs[k]
        return len(old)

    def get_state(self):
        return dict(self.docs.get(store.STATE_DOC, {}))

    def set_state(self, st):
        self.docs[store.STATE_DOC] = {**self.docs.get(store.STATE_DOC, {}), **st}


def row(when, bets, money, key="MLB-824219", phase="sweep"):
    g = dk_games("dk_mlb_p1.html")[0]
    r = main.build_row("MLB", g, match_game("MLB", g, MLB, NOW), when, phase)
    r.update(key=key, ml_home_bets=bets, ml_home_money=money)
    return r


class Saving(unittest.TestCase):
    def test_first_grab_of_the_day_is_the_morning_number(self):
        doc = store.build_doc(row(NOW, 40, 45), None, "2026-09-26")
        self.assertEqual((doc["pubBetsHome"], doc["morningBetsHome"], doc["morningMoneyHome"]), (40, 40, 45))
        self.assertEqual((doc["day"], doc["key"]), ("2026-09-26", "MLB-824219"))

    def test_later_grabs_keep_the_morning_number(self):
        first = store.build_doc(row(NOW, 40, 45), None, "2026-09-26")
        later = store.build_doc(row(NOW + timedelta(hours=1), 52, 51, phase="T-10"), first, "2026-09-26")
        self.assertEqual((later["pubBetsHome"], later["pubMoneyHome"], later["phase"]), (52, 51, "T-10"))
        self.assertEqual((later["morningBetsHome"], later["morningMoneyHome"]), (40, 45))
        self.assertEqual(later["morningAt"], first["morningAt"])

    def test_a_new_day_starts_a_new_morning(self):
        yesterday = store.build_doc(row(NOW, 40, 45), None, "2026-09-25")
        today = store.build_doc(row(NOW, 60, 61), yesterday, "2026-09-26")
        self.assertEqual((today["morningBetsHome"], today["morningMoneyHome"]), (60, 61))

    def test_doc_falls_back_to_spread_then_gives_up(self):
        r = row(NOW, None, None)
        doc = store.build_doc(r, None, "2026-09-26")
        self.assertEqual((doc["market"], doc["moneyline"]), ("spread", None))
        r.update(sp_home_bets=None, sp_home_money=None)
        self.assertIsNone(store.build_doc(r, None, "2026-09-26"))

    def test_robot_only_ever_writes_the_splits_collection(self):
        self.assertEqual(store.COLLECTION, "splits")


class Runs(unittest.TestCase):
    def run_robot(self, start, fs, dry_run=False):
        """One run at `start` with the saved PIT @ DET page; returns (rows, sleeps, log)."""
        g = dk_games("dk_mlb_p1.html")[0]  # PIT @ DET, first pitch 17:10 UTC
        clock = {"t": start}
        slept, rows, logs = [], [], []

        def sleep(sec):
            slept.append(sec)
            clock["t"] += timedelta(seconds=sec)

        saved = official.games_for, dk.fetch_league, store.firestore_from_env, main.summarize
        official.games_for = lambda s, d: MLB if s == "MLB" else []
        dk.fetch_league = lambda s, ed="today": [g]
        store.firestore_from_env = lambda: fs
        main.summarize = lambda r, log: rows.extend(r)
        try:
            main.run(dry_run=dry_run, wait=True, sports=["MLB"], log=logs.append, sleep=sleep, clock=lambda: clock["t"])
        finally:
            official.games_for, dk.fetch_league, store.firestore_from_env, main.summarize = saved
        return rows, slept, logs

    def test_started_games_are_never_recorded(self):
        g = dk_games("dk_mlb_p1.html")[0]
        after_first_pitch = datetime(2026, 9, 26, 17, 11, tzinfo=timezone.utc)
        rows = main.grab(["MLB"], {"MLB": MLB}, after_first_pitch, None, lambda *_: None,
                         fetch_league=lambda s, ed: [g])
        self.assertEqual(rows, [])

    def test_an_empty_dk_answer_gets_one_retry(self):
        g = dk_games("dk_mlb_p1.html")[0]
        answers = [[], [g]]  # 10/3 06:10 UTC: empty once, then the games
        calls = []

        def fetch(sport, ed):
            calls.append(ed)
            return answers.pop(0) if answers else [g]

        rows = main.grab(["MLB"], {"MLB": MLB}, NOW, None, lambda *_: None, fetch_league=fetch, retry_wait=0)
        self.assertEqual(len(calls), 2)
        self.assertEqual([r["key"] for r in rows], ["MLB-824219"])

    def test_pre_game_grabs_at_15_and_10_minutes(self):
        rows, slept, _ = self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), None, dry_run=True)
        det = [r for r in rows if r["key"] == "MLB-824219"]
        self.assertEqual([r["phase"] for r in det], ["sweep", "T-15", "T-10"])
        self.assertEqual([r["min_to_start"] for r in det], [25, 15, 10])
        self.assertEqual(slept, [600.0, 300.0])

    def test_live_run_saves_today_keeps_morning_and_clears_yesterday(self):
        fs = FakeFirestore({
            "MLB-111": {"key": "MLB-111", "day": "2026-09-25"},  # yesterday's game
            "MLB-824219": {"key": "MLB-824219", "day": "2026-09-26", "market": "moneyline",
                           "morningBetsHome": 30, "morningMoneyHome": 35, "morningAt": "2026-09-26T04:07:00Z"},
        })
        self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), fs)
        self.assertNotIn("MLB-111", fs.docs)  # current day only
        det = fs.docs["MLB-824219"]
        self.assertEqual((det["pubBetsHome"], det["pubMoneyHome"], det["phase"]), (52, 51, "T-10"))
        self.assertEqual((det["morningBetsHome"], det["morningMoneyHome"]), (30, 35))  # the midnight number stands
        self.assertEqual([w["phase"] for w in fs.writes if w["key"] == "MLB-824219"], ["sweep", "T-15", "T-10"])

    def test_app_switch_off_pauses_fanduel_but_keeps_the_free_splits(self):
        import os
        pulls = []
        orig_credits, orig_fetch = fanduel.credits_left, fanduel.fetch
        fanduel.credits_left = lambda key: 400
        fanduel.fetch = lambda *a, **k: (pulls.append(a[0]), ([], 400))[1]
        os.environ["ODDS_API_KEY"] = "k"
        try:
            off = FakeFirestore(auto_fill=False)
            _, _, logs = self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), off)
            self.assertEqual(pulls, [])  # no FanDuel credits on an "off" day
            self.assertTrue(any("PAUSED" in x for x in logs))
            self.assertIn("MLB-824219", off.docs)  # the DK splits still saved
            for state in (True, None):  # on, or never set (no rule yet) -> pulls as before
                self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), FakeFirestore(auto_fill=state))
            self.assertTrue(pulls)
        finally:
            fanduel.credits_left, fanduel.fetch = orig_credits, orig_fetch
            del os.environ["ODDS_API_KEY"]

    def test_robot_until_stops_the_next_day(self):
        import os
        fs = FakeFirestore({"MLB-111": {"key": "MLB-111", "day": "2026-09-25"}})
        os.environ["ROBOT_UNTIL"] = "2026-09-25"
        try:
            rows, _, logs = self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), fs)
        finally:
            del os.environ["ROBOT_UNTIL"]
        self.assertEqual(rows, [])
        self.assertEqual(fs.writes, [])
        self.assertTrue(any("STOPPED" in x for x in logs))
        # on the last day itself it still runs
        os.environ["ROBOT_UNTIL"] = "2026-09-26"
        try:
            rows, _, _ = self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), FakeFirestore())
        finally:
            del os.environ["ROBOT_UNTIL"]
        self.assertTrue(rows)

    def test_no_key_saves_nothing(self):
        _, _, logs = self.run_robot(datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc), None)
        self.assertTrue(any("nothing will be saved" in x for x in logs))

    def test_the_day_starts_at_midnight_eastern(self):
        self.assertEqual(str(main.slate_day(datetime(2026, 9, 27, 3, 59, tzinfo=timezone.utc))), "2026-09-26")  # 11:59 PM ET
        self.assertEqual(str(main.slate_day(datetime(2026, 9, 27, 4, 1, tzinfo=timezone.utc))), "2026-09-27")  # 12:01 AM ET


if __name__ == "__main__":
    unittest.main()
