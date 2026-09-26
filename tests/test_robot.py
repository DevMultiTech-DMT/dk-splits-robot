"""Robot tests, built on REAL pages saved on 9/26 (tests/fixtures/).

  python -m unittest discover -s tests -v
"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from robot import dk, main, official, store
from robot.match import match_game, team_split

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


class Saving(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = store.DATA_DIR
        store.DATA_DIR = Path(self.tmp.name)

    def tearDown(self):
        store.DATA_DIR = self.orig
        self.tmp.cleanup()

    def row(self, when, bets, money, key="MLB-824219"):
        g = dk_games("dk_mlb_p1.html")[0]
        r = main.build_row("MLB", g, match_game("MLB", g, MLB, NOW), when, "sweep")
        r.update(key=key, ml_home_bets=bets, ml_home_money=money)
        return r

    def test_csv_round_trip_keeps_numbers_as_numbers(self):
        r = self.row(NOW, 52, 51)
        store.append_rows("2026-09-26", [r])
        back = store.load_rows("2026-09-26")[0]
        self.assertEqual((back["ml_home_bets"], back["sp_home_line"], back["key"]), (52, 1.5, "MLB-824219"))

    def test_doc_keeps_the_morning_number_and_the_latest(self):
        morning = self.row(NOW, 40, 45)
        later = self.row(NOW + timedelta(hours=1), 52, 51)
        later["phase"] = "T-10"
        doc = store.build_doc(later, [morning])
        self.assertEqual((doc["pubBetsHome"], doc["pubMoneyHome"], doc["phase"]), (52, 51, "T-10"))
        self.assertEqual((doc["morningBetsHome"], doc["morningMoneyHome"]), (40, 45))
        self.assertEqual(doc["key"], "MLB-824219")
        self.assertEqual(store.build_doc(morning, [])["morningBetsHome"], 40)  # first grab = morning

    def test_doc_falls_back_to_spread_then_gives_up(self):
        r = self.row(NOW, None, None)
        self.assertEqual(store.build_doc(r, [])["market"], "spread")
        r.update(sp_home_bets=None, sp_home_money=None)
        self.assertIsNone(store.build_doc(r, []))

    def test_robot_only_ever_writes_the_splits_collection(self):
        self.assertEqual(store.COLLECTION, "splits")


class Runs(unittest.TestCase):
    def test_started_games_are_never_recorded(self):
        g = dk_games("dk_mlb_p1.html")[0]
        after_first_pitch = datetime(2026, 9, 26, 17, 11, tzinfo=timezone.utc)
        rows = main.grab(["MLB"], {"MLB": MLB}, after_first_pitch, None, lambda *_: None,
                         fetch_league=lambda s, ed: [g])
        self.assertEqual(rows, [])

    def test_pre_game_grabs_at_15_and_10_minutes(self):
        g = dk_games("dk_mlb_p1.html")[0]  # PIT @ DET, first pitch 17:10 UTC
        clock = {"t": datetime(2026, 9, 26, 16, 45, tzinfo=timezone.utc)}
        slept = []

        def sleep(sec):
            slept.append(sec)
            clock["t"] += timedelta(seconds=sec)

        orig_games, orig_fetch, orig_load = official.games_for, dk.fetch_league, store.load_rows
        official.games_for = lambda s, d: MLB if s == "MLB" else []
        dk.fetch_league = lambda s, ed="today": [g]
        store.load_rows = lambda day: []
        rows = []
        try:
            orig_summ = main.summarize
            main.summarize = lambda r, log: rows.extend(r)
            main.run(dry_run=True, wait=True, sports=["MLB"], log=lambda *_: None, sleep=sleep, clock=lambda: clock["t"])
        finally:
            official.games_for, dk.fetch_league, store.load_rows = orig_games, orig_fetch, orig_load
            main.summarize = orig_summ
        det = [r for r in rows if r["key"] == "MLB-824219"]
        self.assertEqual([r["phase"] for r in det], ["sweep", "T-15", "T-10"])
        self.assertEqual([r["min_to_start"] for r in det], [25, 15, 10])
        self.assertEqual(slept, [600.0, 300.0])

    def test_slate_day_rolls_over_at_7am_eastern(self):
        self.assertEqual(str(main.slate_day(datetime(2026, 9, 27, 4, 30, tzinfo=timezone.utc))), "2026-09-26")  # 12:30 AM ET
        self.assertEqual(str(main.slate_day(datetime(2026, 9, 27, 11, 5, tzinfo=timezone.utc))), "2026-09-27")  # 7:05 AM ET


if __name__ == "__main__":
    unittest.main()
