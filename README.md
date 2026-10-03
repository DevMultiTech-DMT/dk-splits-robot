# dk-splits-robot

Grabs the public DraftKings betting splits (% of the **money** and % of the
**bets** on each side) and **FanDuel's moneylines** for MLB, NFL, WNBA, NBA,
college football and NHL, so the PropEdge app doesn't need them typed in by hand.

## What it does when it's on: two kinds of grabs, nothing else

1. **The morning grab, just after midnight Eastern:** every game of the new day.
   These are the day's **morning** numbers, the "was" in the app's "was → now".
2. **Ten minutes before each game:** that game's numbers again. Games of the same
   sport starting within 6 minutes of each other share one grab, taken no later
   than 3 minutes before the first of them.

- It runs on GitHub's computers in the US. Where you are doesn't matter, and your
  phone and computer can be off.
- Between grabs, one run **sleeps and wakes itself on time**. GitHub's own timer is
  best-effort; on 10/3 it skipped every slot for an hour. GitHub ends a job at 6
  hours, so the robot starts a fresh run before that, and the fresh run carries on
  with the same plan (`splits/_robot_plan`). The hourly timer is only a backup that
  restarts the robot if it ever stops, and a restart never repeats a grab.
- Each game is matched to the **same game ID the app uses** (MLB's gamePk, ESPN's
  event id). If it can't tell which game is which, it leaves it blank and never
  guesses. Nothing is recorded once a game has started.
- **Current day only.** It saves to one place, the Firebase `splits` collection,
  one record per game (latest + morning numbers). Every record from an earlier
  day is deleted on the next run. No history is kept anywhere.

## FanDuel moneylines (The Odds API) and the credit budget

Off until the `ODDS_API_KEY` secret is added, even when the robot is on.

- **Cost:** 1 credit per sport per pull (2 for MLB and NHL, which also carry the
  run line / puck line). A sport with no games left today costs nothing.
- **Credits reset on the 1st of each month at 12 AM UTC** (8 PM Eastern on the
  month's last evening), per the account page. Each day's budget is the credits
  left, spread over the days until that reset. On the last day everything left
  can be spent, because unused credits don't carry over. If the reset lands
  mid-day, the new month's budget starts right then.
- **Free plan (500 credits a month):** about 16 credits a day, which covers the
  **morning line** for every sport plus a **10-minute line** before kickoffs until
  the day's credits run out. The morning line is always taken first.
- **20K plan ($30 a month):** the budget grows automatically, since the robot reads
  the credits left, and every 10-minute line fits. Nothing needs changing.
- The robot never goes below 10 credits. `check-odds` shows the credits left,
  the days to the reset and today's budget, and it's free.
- Each pull matches FanDuel's team names to the app's own game IDs, the same way
  as the splits.

## Switches

| Switch | Where | Now |
|---|---|---|
| Robot on/off | Actions tab → **DK splits robot** → `...` → Enable / Disable workflow | **On** (since 10/3) |
| Firebase key | Settings → Secrets and variables → Actions → `FIREBASE_SERVICE_ACCOUNT` | Added (database-only key) |
| FanDuel lines | same place → secret `ODDS_API_KEY` | Added |
| Auto-fill (everyday on/off) | the **Auto-fill** switch in the app's Picks header (saved to `settings/app`; the robot skips FanDuel while it's off) | On |

The robot switches work from any browser or the GitHub phone app, anywhere in the world.

## Checking it by hand

Actions → **DK splits robot** → **Run workflow** (only while the workflow is
enabled), then pick a mode:
- **look-only**: prints today's numbers and saves nothing
- **check-firebase**: proves the key works (writes, reads back and deletes one
  throwaway record; touches no game data)
- **check-odds**: proves the Odds API key works and shows the credits left (free)
- **live**: one real run

## If something goes wrong

- **Turn it off:** Disable workflow (above). It stops immediately, and the app
  simply shows no DK numbers.
- **Key leaked or not needed anymore:** Google Cloud → IAM & Admin → Service
  Accounts → `dk-splits-robot` → Keys → delete. It stops working instantly.
- **A game shows no numbers:** the run's log lists "could not match" games. Add
  the spelling to `CFB_ALIASES` in `robot/match.py`.

## Files

- `robot/dk.py`: reads the DraftKings page
- `robot/official.py`: official schedules (the app's game ids)
- `robot/match.py`: DraftKings game → official game (name-fix list: `CFB_ALIASES`)
- `robot/fanduel.py`: FanDuel moneylines from The Odds API, plus the credit budget
- `robot/store.py`: Firebase (current day only)
- `robot/main.py`: one run (clean, sweep, pre-game grabs, FanDuel pulls)
- `tests/`: built on real pages and responses saved 9/26 and 10/3/2026. Run with
  `python -m unittest discover -s tests -t .`
