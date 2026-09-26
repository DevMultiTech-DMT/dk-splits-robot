"""Where grabs go.

1. data/YYYY-MM-DD.csv (Eastern date) in this repo -- the permanent history, one
   row per game per grab. DK keeps NO history of its own, so this file is the
   only record of what the numbers were at 9am vs. 10 minutes before the start.
2. Firestore `splits/{SPORT}-{gameId}` -- the latest pre-start numbers + the
   morning numbers, for the PropEdge app. ONLY when the FIREBASE_SERVICE_ACCOUNT
   secret exists. Written with merge; the robot never touches any other
   collection (the app's `bets` / `props` / `mob` ledgers are off-limits).
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
COLLECTION = "splits"  # the ONLY collection the robot may write

COLUMNS = [
    "grabbed_at_utc", "sport", "game_id", "key", "dk_event_id", "start_utc", "phase",
    "min_to_start", "match", "away_dk", "home_dk",
    "ml_home_money", "ml_home_bets", "ml_away_money", "ml_away_bets",
    "sp_home_line", "sp_home_money", "sp_home_bets", "sp_away_money", "sp_away_bets",
    "tot_line", "over_money", "over_bets", "under_money", "under_bets",
]
INT_COLS = {c for c in COLUMNS if c.endswith(("_money", "_bets"))} | {"min_to_start"}
FLOAT_COLS = {"sp_home_line", "tot_line"}


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def csv_path(et_day: str) -> Path:
    return DATA_DIR / f"{et_day}.csv"


def _typed(row: dict) -> dict:
    out = {}
    for k in COLUMNS:
        v = row.get(k, "")
        if v in ("", None):
            out[k] = None
        elif k in INT_COLS:
            out[k] = int(float(v))
        elif k in FLOAT_COLS:
            out[k] = float(v)
        else:
            out[k] = v
    return out


def load_rows(et_day: str) -> list[dict]:
    p = csv_path(et_day)
    if not p.exists():
        return []
    with p.open(newline="", encoding="utf-8") as f:
        return [_typed(r) for r in csv.DictReader(f)]


def append_rows(et_day: str, rows: list[dict]) -> None:
    if not rows:
        return
    p = csv_path(et_day)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with p.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in COLUMNS})


def _market(row: dict) -> str | None:
    if row.get("ml_home_bets") is not None and row.get("ml_home_money") is not None:
        return "moneyline"
    if row.get("sp_home_bets") is not None and row.get("sp_home_money") is not None:
        return "spread"
    return None


_PREFIX = {"moneyline": "ml", "spread": "sp"}


def build_doc(row: dict, history: list[dict]) -> dict | None:
    """Firestore doc for one matched game. `history` = today's earlier rows (any game)."""
    market = _market(row)
    if not market or not row.get("key"):
        return None
    p = _PREFIX[market]
    morning = next(
        (r for r in history if r.get("key") == row["key"] and r.get(f"{p}_home_bets") is not None),
        row,  # first grab of the day IS the morning number
    )
    doc = {
        "source": "dknetwork",
        "sport": row["sport"],
        "gameId": row["game_id"],
        "key": row["key"],
        "dkEventId": row["dk_event_id"],
        "startTime": row["start_utc"],
        "awayDk": row["away_dk"],
        "homeDk": row["home_dk"],
        "market": market,
        "pubBetsHome": row[f"{p}_home_bets"],
        "pubMoneyHome": row[f"{p}_home_money"],
        "grabbedAt": row["grabbed_at_utc"],
        "phase": row["phase"],
        "morningBetsHome": morning[f"{p}_home_bets"],
        "morningMoneyHome": morning[f"{p}_home_money"],
        "morningAt": morning["grabbed_at_utc"],
        "updatedAt": int(time.time() * 1000),
    }
    if row.get("ml_home_bets") is not None:
        doc["moneyline"] = {
            "betsHome": row["ml_home_bets"], "moneyHome": row["ml_home_money"],
            "betsAway": row["ml_away_bets"], "moneyAway": row["ml_away_money"],
        }
    if row.get("sp_home_bets") is not None:
        doc["spread"] = {
            "lineHome": row["sp_home_line"],
            "betsHome": row["sp_home_bets"], "moneyHome": row["sp_home_money"],
            "betsAway": row["sp_away_bets"], "moneyAway": row["sp_away_money"],
        }
    if row.get("over_bets") is not None:
        doc["total"] = {
            "line": row["tot_line"],
            "betsOver": row["over_bets"], "moneyOver": row["over_money"],
            "betsUnder": row["under_bets"], "moneyUnder": row["under_money"],
        }
    return doc


class Firestore:
    """Writes `splits` docs. Constructed only when the service-account secret exists."""

    def __init__(self, service_account_json: str):
        import firebase_admin  # lazy: tests and dry runs never need it
        from firebase_admin import credentials, firestore

        info = json.loads(service_account_json)
        app = firebase_admin.initialize_app(credentials.Certificate(info), name="dk-splits-robot")
        self.db = firestore.client(app)
        self.project = info.get("project_id")

    def write(self, docs: list[dict]) -> int:
        assert COLLECTION == "splits"
        batch, n = self.db.batch(), 0
        for d in docs:
            batch.set(self.db.collection(COLLECTION).document(d["key"]), d, merge=True)
            n += 1
            if n % 400 == 0:
                batch.commit()
                batch = self.db.batch()
        batch.commit()
        return n


def firestore_from_env() -> Firestore | None:
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    return Firestore(raw) if raw else None


def commit_data(message: str) -> bool:
    """Commit + push data/ (GitHub Actions only). Returns True when something was pushed."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return False
    root = DATA_DIR.parent

    def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True)

    git("add", "data")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return False
    git("commit", "-q", "-m", message)
    for _ in range(3):
        if git("push", "-q", check=False).returncode == 0:
            return True
        git("pull", "-q", "--rebase", check=False)
    raise RuntimeError("could not push data commit")
