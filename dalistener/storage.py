from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .models import CaptureSelection, Stability, TranscriptEvent


class SessionStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.RLock()
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                capture_selection TEXT NOT NULL,
                model_name TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS transcript_events (
                session_id TEXT NOT NULL,
                source_id TEXT NOT NULL,
                utterance_id TEXT NOT NULL,
                text TEXT NOT NULL,
                start_ms INTEGER NOT NULL,
                end_ms INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                stability TEXT NOT NULL,
                detected_language TEXT,
                language_probability REAL,
                PRIMARY KEY (session_id, source_id, utterance_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS bookmarks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                timestamp_ms INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            );
        """)
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(transcript_events)")
        }
        if "detected_language" not in columns:
            self.connection.execute("ALTER TABLE transcript_events ADD COLUMN detected_language TEXT")
        if "language_probability" not in columns:
            self.connection.execute("ALTER TABLE transcript_events ADD COLUMN language_probability REAL")
        self.connection.commit()

    def start_session(self, session_id: str, selection: CaptureSelection, model_name: str) -> None:
        selection_dict = asdict(selection)
        selection_dict["mode"] = selection.mode.value
        selection_dict["language"] = selection.language.value
        with self.lock:
            self.connection.execute(
                "INSERT INTO sessions(id, started_at, capture_selection, model_name) VALUES (?, ?, ?, ?)",
                (session_id, datetime.now(timezone.utc).isoformat(), json.dumps(selection_dict), model_name),
            )
            self.connection.commit()

    def end_session(self, session_id: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
            self.connection.commit()

    def abort_session(self, session_id: str) -> None:
        """Remove a session that never successfully began capturing."""
        with self.lock:
            self.connection.execute("DELETE FROM transcript_events WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM bookmarks WHERE session_id = ?", (session_id,))
            self.connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            self.connection.commit()

    def save_event(self, event: TranscriptEvent) -> bool:
        with self.lock:
            existing = self.connection.execute(
                "SELECT stability, revision FROM transcript_events WHERE session_id=? AND source_id=? AND utterance_id=?",
                (event.session_id, event.source_id.value, event.utterance_id),
            ).fetchone()
            if existing and existing["stability"] == Stability.FINAL.value:
                return False
            if existing and existing["revision"] >= event.revision:
                return False
            self.connection.execute("""
                INSERT INTO transcript_events(
                    session_id, source_id, utterance_id, text, start_ms, end_ms, revision, stability,
                    detected_language, language_probability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, source_id, utterance_id) DO UPDATE SET
                    text=excluded.text, start_ms=excluded.start_ms, end_ms=excluded.end_ms,
                    revision=excluded.revision, stability=excluded.stability,
                    detected_language=excluded.detected_language,
                    language_probability=excluded.language_probability
            """, (
                event.session_id, event.source_id.value, event.utterance_id, event.text,
                event.start_ms, event.end_ms, event.revision, event.stability.value,
                event.detected_language, event.language_probability,
            ))
            self.connection.commit()
            return True

    def add_bookmark(self, session_id: str, timestamp_ms: int, note: str = "") -> None:
        with self.lock:
            self.connection.execute(
                "INSERT INTO bookmarks(session_id, timestamp_ms, note) VALUES (?, ?, ?)",
                (session_id, timestamp_ms, note),
            )
            self.connection.commit()

    def events(self, session_id: str, finals_only: bool = False) -> list[sqlite3.Row]:
        where = "AND stability='final'" if finals_only else ""
        with self.lock:
            return list(self.connection.execute(f"""
                SELECT * FROM transcript_events
                WHERE session_id=? {where}
                ORDER BY start_ms, source_id, utterance_id
            """, (session_id,)))

    def bookmarks(self, session_id: str) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.connection.execute(
                "SELECT * FROM bookmarks WHERE session_id=? ORDER BY timestamp_ms", (session_id,),
            ))

    def list_sessions(self, limit: int = 30) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.connection.execute("""
                SELECT s.*, COUNT(e.utterance_id) AS event_count
                FROM sessions s LEFT JOIN transcript_events e ON e.session_id=s.id
                GROUP BY s.id ORDER BY s.started_at DESC LIMIT ?
            """, (limit,)))

    def close(self) -> None:
        with self.lock:
            self.connection.close()


class TranscriptExporter:
    def __init__(self, store: SessionStore):
        self.store = store

    @staticmethod
    def _timestamp(milliseconds: int, srt: bool = False) -> str:
        hours, rest = divmod(milliseconds, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        seconds, millis = divmod(rest, 1_000)
        separator = "," if srt else "."
        return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{millis:03}"

    def export(self, session_id: str, path: Path) -> None:
        # A crash may leave the latest utterance as a draft. Export the latest
        # stored revision rather than silently dropping the user's text.
        rows = self.store.events(session_id, finals_only=False)
        suffix = path.suffix.lower()
        labels = {"microphone": "Me", "system": "System", "status": "Status"}
        if suffix == ".json":
            content = json.dumps([dict(row) for row in rows], indent=2, ensure_ascii=False)
        elif suffix == ".md":
            content = "# DaListener transcript\n\n" + "\n\n".join(
                f"**[{self._timestamp(row['start_ms'])}] {labels[row['source_id']]}:** {row['text']}"
                for row in rows
            )
        elif suffix in (".srt", ".vtt"):
            blocks = []
            for index, row in enumerate(rows, 1):
                start = self._timestamp(row["start_ms"], srt=suffix == ".srt")
                end = self._timestamp(max(row["end_ms"], row["start_ms"] + 1000), srt=suffix == ".srt")
                heading = str(index) if suffix == ".srt" else f"{labels[row['source_id']]}-{index}"
                blocks.append(f"{heading}\n{start} --> {end}\n[{labels[row['source_id']]}] {row['text']}")
            content = ("WEBVTT\n\n" if suffix == ".vtt" else "") + "\n\n".join(blocks) + "\n"
        else:
            content = "\n".join(
                f"[{self._timestamp(row['start_ms'])}] {labels[row['source_id']]}: {row['text']}"
                for row in rows
            )
        path.write_text(content, encoding="utf-8")
