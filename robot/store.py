"""Where grabs go: Firestore `splits/{SPORT}-{gameId}`, CURRENT DAY ONLY.

User's call 9/26: keep only the current day. Each game's doc holds the latest
pre-start numbers plus the day's MORNING numbers (the first grab after
midnight Eastern), and every doc from an earlier day is deleted on the next run.
No history is kept anywhere else. The robot never writes any other collection:
the app's `bets` / `props` / `mob` ledgers are off-limits.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

COLLECTION = "splits"  # the ONLY collection the robot may write
CHECK_DOC = "_robot_check"  # self-test doc; never a game key, so the app never reads it


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _market(row: dict) -> str | None:
    if row.get("ml_home_bets") is not None and row.get("ml_home_money") is not None:
        return "moneyline"
    if row.get("sp_home_bets") is not None and row.get("sp_home_money") is not None:
        return "spread"
    return None


_PREFIX = {"moneyline": "ml", "spread": "sp"}


def _is_pct(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 100


def build_doc(row: dict, existing: dict | None, day: str) -> dict | None:
    """Firestore doc for one matched game.

    `existing` = the game's current doc (or None). Its morning numbers are kept only
    when they are from the SAME day and the SAME market; otherwise this grab becomes
    the morning number."""
    market = _market(row)
    if not market or not row.get("key"):
        return None
    p = _PREFIX[market]
    keep = (
        existing is not None
        and existing.get("day") == day
        and existing.get("market") == market
        and _is_pct(existing.get("morningBetsHome"))
        and _is_pct(existing.get("morningMoneyHome"))
        and isinstance(existing.get("morningAt"), str)
    )
    doc = {
        "source": "dknetwork",
        "day": day,
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
        "morningBetsHome": existing["morningBetsHome"] if keep else row[f"{p}_home_bets"],
        "morningMoneyHome": existing["morningMoneyHome"] if keep else row[f"{p}_home_money"],
        "morningAt": existing["morningAt"] if keep else row["grabbed_at_utc"],
        "updatedAt": int(time.time() * 1000),
    }
    doc["moneyline"] = (
        {
            "betsHome": row["ml_home_bets"], "moneyHome": row["ml_home_money"],
            "betsAway": row["ml_away_bets"], "moneyAway": row["ml_away_money"],
        }
        if row.get("ml_home_bets") is not None
        else None
    )
    doc["spread"] = (
        {
            "lineHome": row["sp_home_line"],
            "betsHome": row["sp_home_bets"], "moneyHome": row["sp_home_money"],
            "betsAway": row["sp_away_bets"], "moneyAway": row["sp_away_money"],
        }
        if row.get("sp_home_bets") is not None
        else None
    )
    doc["total"] = (
        {
            "line": row["tot_line"],
            "betsOver": row["over_bets"], "moneyOver": row["over_money"],
            "betsUnder": row["under_bets"], "moneyUnder": row["under_money"],
        }
        if row.get("over_bets") is not None
        else None
    )
    return doc


class Firestore:
    """The robot's only door to the database. Built only when the key secret exists."""

    def __init__(self, service_account_json: str):
        import firebase_admin  # lazy: tests and look-only runs never need it
        from firebase_admin import credentials, firestore

        info = json.loads(service_account_json)
        app = firebase_admin.initialize_app(credentials.Certificate(info), name="dk-splits-robot")
        self.db = firestore.client(app)
        self.project = info.get("project_id")

    def _col(self):
        assert COLLECTION == "splits"
        return self.db.collection(COLLECTION)

    def existing(self, keys: list[str]) -> dict[str, dict]:
        if not keys:
            return {}
        out = {}
        for snap in self.db.get_all([self._col().document(k) for k in keys]):
            if snap.exists:
                out[snap.id] = snap.to_dict()
        return out

    def write(self, docs: list[dict]) -> int:
        batch, n = self.db.batch(), 0
        for d in docs:
            batch.set(self._col().document(d["key"]), d, merge=True)
            n += 1
            if n % 400 == 0:
                batch.commit()
                batch = self.db.batch()
        batch.commit()
        return n

    def delete_before(self, day: str) -> int:
        """Current day only: remove every doc from an earlier day."""
        try:
            from google.cloud.firestore_v1.base_query import FieldFilter

            q = self._col().where(filter=FieldFilter("day", "<", day))
        except ImportError:
            q = self._col().where("day", "<", day)
        batch, n = self.db.batch(), 0
        for snap in q.stream():
            batch.delete(snap.reference)
            n += 1
            if n % 400 == 0:
                batch.commit()
                batch = self.db.batch()
        batch.commit()
        return n

    def self_test(self) -> str:
        """Write, read back, and delete one throwaway doc. Proves the key + permissions."""
        ref = self._col().document(CHECK_DOC)
        stamp = int(time.time() * 1000)
        ref.set({"check": stamp})
        back = ref.get().to_dict() or {}
        ref.delete()
        gone = not ref.get().exists
        return "OK" if back.get("check") == stamp and gone else f"FAILED (read back {back}, deleted={gone})"


def firestore_from_env() -> Firestore | None:
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "").strip()
    return Firestore(raw) if raw else None
