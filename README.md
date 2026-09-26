# dk-splits-robot

Grabs the public DraftKings betting splits (% of the **money** and % of the
**bets** on each side) for MLB, NFL, WNBA, NBA and college football, so the
PropEdge app doesn't need them typed in by hand.

**Status: BUILT, SWITCHED OFF.** Nothing runs until it is turned on.

## What it does when it's on

- Runs **every 30 minutes, around the clock**, on GitHub's computers in the US.
  Where you are doesn't matter, and your phone and computer can be off.
- The **first run after midnight Eastern** takes the day's **morning** numbers.
  Every later run updates the latest numbers.
- For each game it also grabs **15 and 10 minutes before the start**. It waits for
  those moments itself, because GitHub's timer can run late.
- Each game is matched to the **same game ID the app uses** (MLB's gamePk, ESPN's
  event id). If it can't tell which game is which, it leaves it blank and never
  guesses. Nothing is recorded once a game has started.
- **Current day only.** It saves to one place, the Firebase `splits` collection,
  one record per game (latest + morning numbers). Every record from an earlier
  day is deleted on the next run. No history is kept anywhere.

## Switches

| Switch | Where | Now |
|---|---|---|
| Robot on/off | Actions tab → **DK splits robot** → `...` → Enable / Disable workflow | **Off** |
| Firebase key | Settings → Secrets and variables → Actions → `FIREBASE_SERVICE_ACCOUNT` | Added (database-only key) |
| Show / auto-fill in the app | PropEdge `lib/dkSplits.ts` flags | **Off** |

All of these work from any browser or the GitHub phone app, anywhere in the world.

## Checking it by hand

Actions → **DK splits robot** → **Run workflow** (only while the workflow is
enabled), then pick a mode:
- **look-only**: prints today's numbers and saves nothing
- **check-firebase**: proves the key works (writes, reads back and deletes one
  throwaway record; touches no game data)
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
- `robot/store.py`: Firebase (current day only)
- `robot/main.py`: one run (clean, sweep, pre-game grabs)
- `tests/`: built on real pages saved 9/26/2026. Run with `python -m unittest discover -s tests -t .`
