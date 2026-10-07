"""User feedback and reading state; original archive messages remain immutable."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

ACTIONS = {"outdated", "quoted", "excluded", "corrected", "restored"}


class AnnotationStore:
    def __init__(self, root: Path):
        self.path = Path(root) / "memory" / "annotations.sqlite"

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS feedback (
                  id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, action TEXT NOT NULL,
                  note TEXT NOT NULL, stated_at TEXT NOT NULL, valid_from TEXT, valid_until TEXT,
                  assertion_kind TEXT NOT NULL, replacement_message_id TEXT);
                CREATE INDEX IF NOT EXISTS feedback_message ON feedback(message_id, id);
                CREATE TABLE IF NOT EXISTS bookmarks (
                  message_id TEXT PRIMARY KEY, source_id INTEGER NOT NULL,
                  title TEXT NOT NULL, topic TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reading_position (
                  snapshot_key TEXT NOT NULL, conversation_id TEXT NOT NULL,
                  message_id TEXT NOT NULL, updated_at TEXT NOT NULL,
                  PRIMARY KEY(snapshot_key, conversation_id));
                CREATE TABLE IF NOT EXISTS assertions (
                  id INTEGER PRIMARY KEY, message_id TEXT NOT NULL, quote TEXT NOT NULL,
                  kind TEXT NOT NULL, created_at TEXT NOT NULL,
                  UNIQUE(message_id, quote));
            """)
            yield db
            db.commit()
        finally:
            db.close()

    def current(self) -> dict:
        if not self.path.exists():
            return {}
        with self.connect() as db:
            rows = db.execute("SELECT * FROM feedback WHERE id IN (SELECT max(id) FROM feedback GROUP BY message_id)").fetchall()
        return {r["message_id"]: dict(r) for r in rows}

    def feedback(self, message_id: str, action: str, *, note="", valid_from=None,
                 valid_until=None, assertion_kind="statement", replacement_message_id=None):
        if action not in ACTIONS:
            raise ValueError("action must be outdated, quoted, excluded, corrected or restored")
        if assertion_kind not in {"statement", "preference", "plan", "event"}:
            raise ValueError("assertion_kind must be statement, preference, plan or event")
        for value in (valid_from, valid_until):
            if value:
                date.fromisoformat(value)
        if valid_from and valid_until and date.fromisoformat(valid_from) > date.fromisoformat(valid_until):
            raise ValueError("valid_until must not precede valid_from")
        if action == "corrected" and not note.strip() and not replacement_message_id:
            raise ValueError("corrected feedback requires a note or replacement message")
        if action == "outdated" and not valid_until:
            valid_until = datetime.now(timezone.utc).date().isoformat()
        with self.connect() as db:
            cursor = db.execute("""INSERT INTO feedback(message_id, action, note, stated_at,
                valid_from, valid_until, assertion_kind, replacement_message_id) VALUES(?,?,?,?,?,?,?,?)""",
                (message_id, action, note, datetime.now(timezone.utc).isoformat(), valid_from,
                 valid_until, assertion_kind, replacement_message_id))
            return dict(db.execute("SELECT * FROM feedback WHERE id=?", (cursor.lastrowid,)).fetchone())

    def history(self, message_id):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM feedback WHERE message_id=? ORDER BY id", (message_id,))]

    def bookmark(self, message_id, source_id, title, *, topic="", note="", remove=False):
        with self.connect() as db:
            if remove:
                db.execute("DELETE FROM bookmarks WHERE message_id=?", (message_id,))
            else:
                db.execute("INSERT OR REPLACE INTO bookmarks VALUES(?,?,?,?,?,?)",
                           (message_id, source_id, title, topic.strip(), note, datetime.now(timezone.utc).isoformat()))
        return {"saved": not remove}

    def bookmarks(self, topic=None):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM bookmarks WHERE (? IS NULL OR topic=?) ORDER BY created_at DESC", (topic, topic))]

    def position(self, snapshot_key, conversation_id, message_id=None):
        with self.connect() as db:
            if message_id:
                db.execute("INSERT OR REPLACE INTO reading_position VALUES(?,?,?,?)",
                           (snapshot_key, conversation_id, message_id, datetime.now(timezone.utc).isoformat()))
            row = db.execute("SELECT * FROM reading_position WHERE snapshot_key=? AND conversation_id=?", (snapshot_key, conversation_id)).fetchone()
            return dict(row) if row else None

    def remember(self, message_id, quote, kind="statement"):
        if kind not in {"statement", "preference", "plan", "event"}:
            raise ValueError("kind must be statement, preference, plan or event")
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO assertions(message_id, quote, kind, created_at) VALUES(?,?,?,?)",
                       (message_id, quote, kind, datetime.now(timezone.utc).isoformat()))
            return dict(db.execute("SELECT * FROM assertions WHERE message_id=? AND quote=?", (message_id, quote)).fetchone())

    def assertions(self):
        if not self.path.exists():
            return []
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM assertions ORDER BY id DESC")]
