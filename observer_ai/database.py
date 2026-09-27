"""SQLite persistence layer for Observer AI events and snapshots."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from .models import ActivityEvent, ContextSnapshot  # noqa: F401

_DEFAULT_DB = Path("observer.db")


_lock = threading.Lock()


def connect(db_path: str | Path = _DEFAULT_DB) -> sqlite3.Connection:
    """Open (and migrate) the events database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``check_same_thread=False`` because collectors run in worker threads
    # but write through this same connection. Concurrency at the Python
    # level is already handled by the module-level ``_lock`` below, and
    # SQLite itself serializes actual writes.
    conn = sqlite3.connect(path, timeout=5.0, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            source TEXT NOT NULL,
            event_type TEXT NOT NULL,
            application TEXT,
            window_title TEXT,
            project TEXT,
            project_path TEXT,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp DESC);
        CREATE INDEX IF NOT EXISTS idx_events_project ON events(project);

        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            current_application TEXT,
            current_project TEXT,
            current_branch TEXT,
            likely_activity TEXT,
            confidence INTEGER,
            recent_progress_json TEXT,
            blockers_json TEXT,
            open_loops_json TEXT,
            recommended_next_step TEXT
        );
        """
    )


def record_event(conn: sqlite3.Connection, event: ActivityEvent) -> None:
    import json

    with _lock:
        conn.execute(
            "INSERT INTO events (timestamp, source, event_type, application, "
            "window_title, project, project_path, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event.timestamp,
                event.source,
                event.event_type.value,
                event.application,
                event.window_title,
                event.project,
                event.project_path,
                json.dumps(event.metadata, ensure_ascii=False),
            ),
        )


def record_snapshot(conn: sqlite3.Connection, snapshot: ContextSnapshot) -> None:
    import json

    with _lock:
        conn.execute(
            "INSERT INTO snapshots (timestamp, current_application, current_project, "
            "current_branch, likely_activity, confidence, recent_progress_json, "
            "blockers_json, open_loops_json, recommended_next_step) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                time.time(),
                snapshot.current_application,
                snapshot.current_project,
                snapshot.current_branch,
                snapshot.likely_activity,
                snapshot.confidence,
                json.dumps(snapshot.recent_progress, ensure_ascii=False),
                json.dumps(snapshot.possible_blockers, ensure_ascii=False),
                json.dumps(
                    [
                        {"confidence": loop.confidence, "summary": loop.summary}
                        for loop in snapshot.open_loops
                    ],
                    ensure_ascii=False,
                ),
                snapshot.recommended_next_step,
            ),
        )
