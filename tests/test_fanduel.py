"""FanDuel moneylines (The Odds API), on REAL responses pulled 10/3 ~1:05 AM ET."""
import unittest
from datetime import datetime, timedelta, timezone

from robot import dk, fanduel, main, official, store
from robot.match import match_game, match_teams

from .test_robot import FakeFirestore, dk_games, load

CFB_1003 = official.parse_espn("CFB", load("espn_cfb_1003.json"))
MLB_1003 = official.parse_mlb(load("mlb_schedule_1003.json"))
NHL_1003 = official.parse_espn("NHL", load("espn_nhl_1003.json"))
NOW_1003 = datetime(2026, 10, 3, 5, 5, tzinfo=timezone.utc)  # 1:05 AM ET, before every game


class FanDuel(unittest.TestCase):
    def test_parse_keeps_only_fanduel_moneylines(self):
        ev = fanduel.parse_events(load("fd_cfb_1003.json"))
        self.assertEqual(len(ev), 59)  # 66 events, 59 carry a FanDuel price
        e = next(x for x in ev if x.home == "Air Force Falcons")
        self.assertEqual((e.away, e.ml_away, e.ml_home), ("Navy Midshipmen", 128, -154))

    def test_every_cfb_game_on_the_app_slate_matches(self):
        today = [e for e in load("fd_cfb_1003.json")
                 if datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00")).astimezone(dk.ET).date().isoformat() == "2026-10-03"]
        got = {}
        for e in today:  # every game listed, priced or not
            t = datetime.fromisoformat(e["commence_time"].replace("Z", "+00:00"))
            m = match_teams("CFB", e["away_team"], e["home_team"], t, CFB_1003)
            self.assertIsNotNone(m, f"{e['away_team']} @ {e['home_team']}")
            got[m.game.key] = m
        self.assertEqual(len(got), 54)  # all 54 games the app showed on 10/3
        umass = next(m for m in got.values() if m.game.home.abbr == "MASS")
        self.assertEqual(umass.note, "both")  # "UMass Minutemen" alias
        # at 1 AM FanDuel priced 52: TXSO @ FAU and McNeese @ LSU had no book at all -> left blank
        priced = [e for e in fanduel.parse_events(load("fd_cfb_1003.json"))
                  if e.start_utc.astimezone(dk.ET).date().isoformat() == "2026-10-03"]
        self.assertEqual(len(priced), 52)

    def test_mlb_matches_by_nickname(self):
        for e in fanduel.parse_events(load("fd_mlb_1003.json")):
            self.assertIsNotNone(match_teams("MLB", e.away, e.home, e.start_utc, MLB_1003), e.home)

    def test_nhl_dk_pages_match(self):
        games = dk_games("dk_nhl_p1.html", "dk_nhl_p2.html")
        self.assertEqual(len(games), 13)
        for g in games:
            self.assertIsNotNone(match_game("NHL", g, NHL_1003, NOW_1003), g.home)

    def test_credits_reset_on_the_1st_at_midnight_utc(self):
        # the user's account page 10/3: "Monthly plans reset on the 1st of each month at 12AM UTC"
        self.assertEqual(fanduel.next_reset(NOW_1003), datetime(2026, 11, 1, tzinfo=timezone.utc))
        self.assertEqual(fanduel.next_reset(datetime(2026, 12, 15, tzinfo=timezone.utc)),
                         datetime(2027, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(fanduel.days_left(NOW_1003), 29)  # 10/3 1:05 AM ET -> 11/1
        # Oct 31, 2 PM ET: 6 hours to the reset -> the whole remainder is today's
        last_day = datetime(2026, 10, 31, 18, 0, tzinfo=timezone.utc)
        self.assertEqual(fanduel.days_left(last_day), 1)
        self.assertEqual(fanduel.day_cap(100, last_day), 90)

    def test_budget(self):
        self.assertEqual(
            (fanduel.day_cap(500, NOW_1003), fanduel.day_cap(497, NOW_1003), fanduel.day_cap(20000, NOW_1003),
             fanduel.day_cap(5, NOW_1003)),
            (16, 16, 689, 0),
        )
        a = fanduel.allowed
        # the two grabs only: the morning line + the 10-minute line
        self.assertTrue(a("morning", 99, 16, 400))  # the morning line ignores the day cap
        self.assertTrue(a("T-10", 5, 16, 400))
        self.assertFalse(a("T-10", 16, 16, 400))  # cap reached
        self.assertFalse(a("sweep", 0, 666, 19000))  # nothing else, on any plan
        # the app's pull-down (user 10/3: "everything, every time"): past the day's cap too
        self.assertTrue(a("refresh", 16, 16, 400))
        self.assertFalse(a("refresh", 0, 16, fanduel.FLOOR))  # the floor still holds
        # nearly out: nothing, not even the morning line
        self.assertFalse(a("morning", 0, 0, fanduel.FLOOR))
        self.assertFalse(a("morning", 0, 16, None))

    def test_fd_doc_keeps_the_morning_line_all_day(self):
        kw = dict(key="MLB-1", sport="MLB", game_id="1", start_utc="2026-10-03T20:00:00Z", book_at="", day="2026-10-03")
        first = store.build_fd_doc(ml_away=188, ml_home=-225, grabbed_at="2026-10-03T04:07:00Z", existing=None, **kw)
        later = store.build_fd_doc(ml_away=170, ml_home=-205, grabbed_at="2026-10-03T19:50:00Z", existing=first, **kw)
        self.assertEqual((later["fdMlAway"], later["fdMlHome"]), (170, -205))
        self.assertEqual((later["fdMorningAway"], later["fdMorningHome"], later["fdMorningAt"]),
                         (188, -225, "2026-10-03T04:07:00Z"))
        nextday = store.build_fd_doc(ml_away=150, ml_home=-180, grabbed_at="2026-10-04T04:07:00Z",
                                     existing=first, **{**kw, "day": "2026-10-04"})
        self.assertEqual(nextday["fdMorningAway"], 150)


    def test_fd_doc_keeps_the_morning_run_and_puck_line(self):
        # user 10/3: spread boxes "not updating properly" -- they had no "was" to show a move
        kw = dict(key="NHL-1", sport="NHL", game_id="1", start_utc="2026-10-03T23:00:00Z", book_at="", day="2026-10-03")
        # CHI @ BUF 10/3: BUF -1.5 went +104 (morning) -> +102 (10-minute grab)
        first = store.build_fd_doc(ml_away=188, ml_home=-230, grabbed_at="2026-10-03T05:34:00Z", existing=None,
                                   sp_away=(1.5, -130), sp_home=(-1.5, 104), **kw)
        self.assertEqual((first["fdSpMorningAway"], first["fdSpMorningHome"]),
                         ({"point": 1.5, "price": -130}, {"point": -1.5, "price": 104}))
        later = store.build_fd_doc(ml_away=198, ml_home=-245, grabbed_at="2026-10-03T22:50:00Z", existing=first,
                                   sp_away=(1.5, -128), sp_home=(-1.5, 102), **kw)
        self.assertEqual(later["fdSpHome"], {"point": -1.5, "price": 102})
        self.assertEqual(later["fdSpMorningHome"], {"point": -1.5, "price": 104})
        # a doc written before the morning field existed: its earlier line becomes the morning
        old = {k: v for k, v in first.items() if not k.startswith("fdSpMorning")}
        self.assertEqual(store.build_fd_doc(ml_away=198, ml_home=-245, grabbed_at="2026-10-03T22:50:00Z", existing=old,
                                            sp_away=(1.5, -128), sp_home=(-1.5, 102), **kw)["fdSpMorningHome"],
                         {"point": -1.5, "price": 104})
        # a new day starts over; a sport with no run line gets no field
        nextday = store.build_fd_doc(ml_away=150, ml_home=-180, grabbed_at="2026-10-04T04:07:00Z", existing=later,
                                     sp_away=(1.5, -140), sp_home=(-1.5, 118), **{**kw, "day": "2026-10-04"})
        self.assertEqual(nextday["fdSpMorningHome"], {"point": -1.5, "price": 118})
        cfb = store.build_fd_doc(ml_away=150, ml_home=-180, grabbed_at="2026-10-04T04:07:00Z", existing=None, **kw)
        self.assertNotIn("fdSpMorningHome", cfb)


class FanDuelPulls(unittest.TestCase):
    def puller(self, fs, credits=480, events=None, calls=None):
        events = events if events is not None else fanduel.parse_events(load("fd_mlb_1003.json"))
        left = {"n": credits}

        def fetch(sport, key, frm, to):
            if calls is not None:
                calls.append((sport, frm))
            got = [e for e in events if frm <= e.start_utc <= to]
            left["n"] -= 1 if got else 0  # a pull with no games costs nothing
            return got, left["n"]

        return main.FanDuelPuller("k", fs, {"MLB": MLB_1003}, "2026-10-03", lambda *_: None, NOW_1003,
                                  fetch=fetch, credits_fn=lambda key: credits)

    def lad_key(self):
        return next(g.key for g in MLB_1003 if g.home.abbr == "LAD")

    def test_morning_then_pre_game_keeps_the_morning_line(self):
        fs = FakeFirestore()
        p = self.puller(fs)
        p.pull("MLB", "morning", NOW_1003)
        d = fs.docs[self.lad_key()]
        self.assertEqual((d["fdMlAway"], d["fdMlHome"]), (188, -225))
        moved = [fanduel.FdGame(e.event_id, e.away, e.home, e.start_utc, 170, -205, "")
                 for e in fanduel.parse_events(load("fd_mlb_1003.json"))]
        self.puller(fs, credits=479, events=moved).pull("MLB", "T-10", NOW_1003 + timedelta(hours=10),
                                                        only_keys={self.lad_key()})
        d = fs.docs[self.lad_key()]
        self.assertEqual((d["fdMlAway"], d["fdMlHome"], d["fdMorningAway"], d["fdMorningHome"]), (170, -205, 188, -225))
        self.assertEqual(fs.docs[store.STATE_DOC]["oddsUsed"], 2)

    def test_the_10_minute_grab_touches_only_the_games_about_to_start(self):
        fs = FakeFirestore()
        self.puller(fs).pull("MLB", "morning", NOW_1003)
        moved = [fanduel.FdGame(e.event_id, e.away, e.home, e.start_utc, 170, -205, "")
                 for e in fanduel.parse_events(load("fd_mlb_1003.json"))]
        self.puller(fs, events=moved).pull("MLB", "T-10", NOW_1003 + timedelta(hours=10), only_keys={self.lad_key()})
        others = [g.key for g in MLB_1003 if g.key != self.lad_key() and g.key in fs.docs]
        self.assertTrue(others)
        for k in others:  # every other game keeps its morning line until its own 10-minute grab
            self.assertEqual(fs.docs[k]["fdMlAway"], fs.docs[k]["fdMorningAway"])

    def test_run_and_puck_lines_ride_the_same_pull(self):
        # The Odds API shape with h2h + spreads (Yankees @ Rays 10/3: NYY +1.5 -210 / TB -1.5 +172)
        ev = {"id": "x", "commence_time": "2026-10-03T22:31:00Z", "home_team": "Tampa Bay Rays",
              "away_team": "New York Yankees", "bookmakers": [{"key": "fanduel", "last_update": "", "markets": [
                  {"key": "h2h", "outcomes": [{"name": "New York Yankees", "price": 114}, {"name": "Tampa Bay Rays", "price": -134}]},
                  {"key": "spreads", "outcomes": [{"name": "New York Yankees", "price": -210, "point": 1.5},
                                                  {"name": "Tampa Bay Rays", "price": 172, "point": -1.5}]}]}]}
        g = fanduel.parse_events([ev])[0]
        self.assertEqual((g.sp_away, g.sp_home), ((1.5, -210), (-1.5, 172)))
        fs = FakeFirestore()
        self.puller(fs, events=[g]).pull("MLB", "morning", NOW_1003)
        tb = next(x.key for x in MLB_1003 if x.home.abbr == "TB")
        self.assertEqual((fs.docs[tb]["fdSpAway"], fs.docs[tb]["fdSpHome"]),
                         ({"point": 1.5, "price": -210}, {"point": -1.5, "price": 172}))

    def test_pull_down_refresh_is_counted_apart_and_never_blocks_the_10_minute_grab(self):
        fs, calls = FakeFirestore(), []
        fs.docs[store.STATE_DOC] = {"day": "2026-10-03", "oddsUsed": 16, "oddsCap": 16, "lastPull": {}}
        p = self.puller(fs, calls=calls)
        p.pull("MLB", "refresh", NOW_1003)  # cap used up: the pull-down still goes
        self.assertEqual(len(calls), 1)
        st = fs.docs[store.STATE_DOC]
        self.assertEqual((st["oddsUsed"], st["refreshUsed"]), (16, 1))  # counted apart
        p.pull("MLB", "refresh", NOW_1003 + timedelta(seconds=40))  # a second pull-down inside a minute
        self.assertEqual(len(calls), 1)
        p.pull("MLB", "refresh", NOW_1003 + timedelta(seconds=70))
        self.assertEqual(len(calls), 2)
        # a pull-down 2 minutes before a 10-minute grab never makes that grab skip (own clock)
        fs2, calls2 = FakeFirestore(), []
        p2 = self.puller(fs2, calls=calls2)
        p2.pull("MLB", "refresh", NOW_1003)
        p2.pull("MLB", "T-10", NOW_1003 + timedelta(minutes=2))
        self.assertEqual(len(calls2), 2)

    def test_fill_in_pull_only_when_listed_at_most_3_a_day_2_hours_apart(self):
        # user 10/4: one MLB game had no FanDuel line all day (not posted at the 12:02 AM grab)
        fs, calls = FakeFirestore(), []
        p = self.puller(fs, calls=calls)
        listed = {"now": []}
        p.events_fn = lambda sport, key, frm, to: listed["now"]
        t = NOW_1003
        self.assertEqual(p.pull("MLB", "fill", t), 0)  # not listed yet: free look only, no pull
        self.assertEqual(calls, [])
        ev = next(e for e in fanduel.parse_events(load("fd_mlb_1003.json")) if "Dodgers" in e.home)
        listed["now"] = [(ev.away, ev.home, ev.start_utc)]
        p.pull("MLB", "fill", t)
        self.assertEqual(len(calls), 1)
        st = fs.docs[store.STATE_DOC]
        self.assertEqual((st["fills"]["MLB"], st.get("oddsUsed", 0)), (1, 0))  # counted apart
        self.assertGreaterEqual(st["fillUsed"], 1)
        p.pull("MLB", "fill", t + timedelta(minutes=90))  # inside 2 hours: no
        self.assertEqual(len(calls), 1)
        for h in (2, 4, 6, 8):
            p.pull("MLB", "fill", t + timedelta(hours=h))
        self.assertEqual(len(calls), 3)  # at most 3 a day

    def test_nba_preseason_key_asked_too_and_nfl_carries_its_spread(self):
        # 10/4: the NBA preseason (UTAH @ DEN, GS @ LAC) is only under basketball_nba_preseason
        urls = []

        def get(url):
            urls.append(url)
            if "preseason" in url and "nfl" in url:
                return 404, '{"message": "Unknown sport"}', {}  # an off-season key: skipped
            return 200, "[]", {"x-requests-remaining": "400"}

        fanduel.fetch("NBA", "k", NOW_1003, NOW_1003 + timedelta(hours=6), get=get)
        self.assertEqual([u.split("/sports/")[1].split("/")[0] for u in urls], ["basketball_nba", "basketball_nba_preseason"])
        urls.clear()
        games, left = fanduel.fetch("NFL", "k", NOW_1003, NOW_1003 + timedelta(hours=6), get=get)
        self.assertEqual((games, left), ([], 400))
        self.assertIn("markets=h2h%2Cspreads", urls[0])  # the NFL spread rides the same pull

    def test_never_the_same_sport_twice_inside_5_minutes(self):
        fs, calls = FakeFirestore(), []
        p = self.puller(fs, calls=calls)
        p.pull("MLB", "morning", NOW_1003)
        p.pull("MLB", "T-10", NOW_1003 + timedelta(minutes=3))  # same sport, too soon
        self.assertEqual(len(calls), 1)

    def test_day_cap_stops_the_10_minute_line_but_never_the_morning(self):
        fs, calls = FakeFirestore(), []
        fs.docs[store.STATE_DOC] = {"day": "2026-10-03", "oddsUsed": 16, "oddsCap": 16, "lastPull": {}}
        p = self.puller(fs, calls=calls)
        p.pull("MLB", "T-10", NOW_1003)
        self.assertEqual(calls, [])
        p.pull("MLB", "morning", NOW_1003)
        self.assertEqual(len(calls), 1)

    def test_the_8pm_reset_starts_the_new_budget_mid_day(self):
        fs, calls = FakeFirestore(), []
        # Oct 31: the day's budget is spent, then 12 AM UTC (8 PM ET) refills the plan
        fs.docs[store.STATE_DOC] = {"day": "2026-10-03", "oddsUsed": 40, "oddsCap": 40,
                                    "creditsAtCap": 50, "lastPull": {}}
        p = self.puller(fs, credits=500, calls=calls)
        self.assertEqual((p.state["oddsUsed"], p.state["creditsAtCap"]), (0, 500))
        p.pull("MLB", "T-10", NOW_1003)
        self.assertEqual(len(calls), 1)

    def test_a_one_day_budget_raise_applies_only_to_that_day(self):
        import os
        fs, calls = FakeFirestore(), []
        fs.docs[store.STATE_DOC] = {"day": "2026-10-03", "oddsUsed": 16, "oddsCap": 16, "creditsAtCap": 480, "lastPull": {}}
        os.environ["ODDS_CAP_TODAY"] = "2026-10-03=36"
        try:
            p = self.puller(fs, calls=calls)
            self.assertEqual(p.state["oddsCap"], 36)
            p.pull("MLB", "T-10", NOW_1003)  # 16 used < 36 -> the 10-minute line still goes
            self.assertEqual(len(calls), 1)
            os.environ["ODDS_CAP_TODAY"] = "2026-10-02=36"  # another day: no raise
            fs.docs[store.STATE_DOC]["oddsCap"] = 16
            self.assertEqual(self.puller(FakeFirestore({store.STATE_DOC: dict(fs.docs[store.STATE_DOC])})).state["oddsCap"], 16)
        finally:
            del os.environ["ODDS_CAP_TODAY"]

    def test_prices_follow_the_team_when_listed_the_other_way_round(self):
        fs = FakeFirestore()
        lad = next(e for e in fanduel.parse_events(load("fd_mlb_1003.json")) if e.home == "Los Angeles Dodgers")
        swapped = fanduel.FdGame(lad.event_id, lad.home, lad.away, lad.start_utc, lad.ml_home, lad.ml_away, "")
        self.puller(fs, events=[swapped]).pull("MLB", "morning", NOW_1003)
        d = fs.docs[self.lad_key()]
        self.assertEqual((d["fdMlAway"], d["fdMlHome"]), (188, -225))

    def test_started_games_get_no_odds(self):
        fs = FakeFirestore()
        after_all = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)
        self.puller(fs).pull("MLB", "morning", after_all)
        self.assertEqual([k for k in fs.docs if k != store.STATE_DOC], [])

    def test_no_key_means_no_pulls(self):
        p = main.FanDuelPuller("", FakeFirestore(), {"MLB": MLB_1003}, "2026-10-03", lambda *_: None, NOW_1003,
                               fetch=lambda *a: self.fail("pulled without a key"))
        p.pull("MLB", "morning", NOW_1003)
        self.assertFalse(p.on)


if __name__ == "__main__":
    unittest.main()
