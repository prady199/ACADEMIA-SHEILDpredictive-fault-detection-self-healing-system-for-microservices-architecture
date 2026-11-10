"""SQLite persistence for incidents, notifications, settings and availability history."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY, opened_at REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, body TEXT);
            CREATE TABLE IF NOT EXISTS minutes(ts INTEGER PRIMARY KEY, ok INTEGER, fail INTEGER);
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self.db.commit()

    # incidents ---------------------------------------------------------------
    def next_incident_id(self) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(id), 0) FROM incidents").fetchone()
        return int(row[0]) + 1

    def save_incident(self, inc: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO incidents(id, opened_at, body) VALUES(?,?,?)",
            (inc["id"], inc["opened_at"], json.dumps(inc)),
        )
        self.db.commit()

    def incidents(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT body FROM incidents ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def incident(self, iid: int) -> dict | None:
        row = self.db.execute("SELECT body FROM incidents WHERE id=?", (iid,)).fetchone()
        return json.loads(row[0]) if row else None

    # notifications -----------------------------------------------------------
    def add_notification(self, note: dict) -> int:
        cur = self.db.execute("INSERT INTO notifications(ts, body) VALUES(?,?)", (note["ts"], json.dumps(note)))
        self.db.commit()
        return int(cur.lastrowid)

    def update_notification(self, nid: int, note: dict) -> None:
        self.db.execute("UPDATE notifications SET body=? WHERE id=?", (json.dumps(note), nid))
        self.db.commit()

    def notifications(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT id, body FROM notifications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{**json.loads(b), "id": i} for i, b in rows]

    # availability ------------------------------------------------------------
    def add_second(self, ts: int, ok: int, fail: int) -> None:
        minute = ts - ts % 60
        self.db.execute(
            "INSERT INTO minutes(ts, ok, fail) VALUES(?,?,?) "
            "ON CONFLICT(ts) DO UPDATE SET ok=ok+excluded.ok, fail=fail+excluded.fail",
            (minute, ok, fail),
        )

    def commit(self) -> None:
        self.db.commit()

    def minutes(self, since: float) -> list[dict]:
        rows = self.db.execute("SELECT ts, ok, fail FROM minutes WHERE ts>=? ORDER BY ts", (since,)).fetchall()
        return [{"ts": r[0], "ok": r[1], "fail": r[2]} for r in rows]

    def totals(self) -> dict:
        ok, fail = self.db.execute("SELECT COALESCE(SUM(ok),0), COALESCE(SUM(fail),0) FROM minutes").fetchone()
        return {"ok": int(ok), "fail": int(fail)}

    # settings ----------------------------------------------------------------
    def get_setting(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, json.dumps(value)))
        self.db.commit()

    def clear_history(self) -> None:
        self.db.executescript("DELETE FROM incidents; DELETE FROM notifications; DELETE FROM minutes;")
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()


def now() -> float:
    return time.time()
