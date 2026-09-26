# dk-splits-robot

Grabs the public DraftKings betting splits (% of the **money** and % of the
**bets** on each side) for MLB, NFL, WNBA, NBA and college football, so the
PropEdge app doesn't need them typed in by hand.

**Status: BUILT, SWITCHED OFF.** Nothing runs until it is turned on.

## What it does when it's on

- Every 30 minutes, 8am to about 12:30am Eastern, it reads every game still to
  start.
- For each game starting soon, it also grabs **15 and 10 minutes before** the
  start. It waits for those moments itself, because GitHub's timer can run late.
- Each game is matched to the **same game ID the app uses** (MLB's gamePk, ESPN's
  event id). If it can't tell which game is which, it leaves it blank and never
  guesses.
- Nothing is recorded once a game has started.
- It saves every grab to `data/YYYY-MM-DD.csv` in this repo. That file is the
  history (DraftKings keeps none), and the first grab of the day is the
  **morning** number.
- It saves to the Firebase `splits` collection (latest + morning numbers) **only
  after** a Firebase key is added. It never writes anywhere else.

## Switches

| Switch | Where | Starts as |
|---|---|---|
| Robot on/off | Actions tab → **DK splits robot** → `...` → Enable / Disable workflow | **Off** |
| Save to Firebase | Settings → Secrets and variables → Actions → secret `FIREBASE_SERVICE_ACCOUNT` | **Off** (no key) |
| Show / auto-fill in the app | PropEdge `lib/dkSplits.ts` flags | **Off** |

## Checking it by hand

Actions → **DK splits robot** → **Run workflow**. The default mode, *look-only*,
prints today's numbers and saves nothing. This only works while the workflow is
enabled.

## Files

- `robot/dk.py`: reads the DraftKings page
- `robot/official.py`: official schedules (the app's game ids)
- `robot/match.py`: DraftKings game → official game (name-fix list: `CFB_ALIASES`)
- `robot/store.py`: CSV history + Firebase
- `robot/main.py`: one run (sweep, then the pre-game grabs)
- `tests/`: built on real pages saved 9/26/2026. Run with `python -m unittest discover -s tests -t .`
