"""Local study activity sampling and cautious behavior summaries."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import datetime as dt
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from observer_ai.collectors import _current_window_title


SAMPLE_SECONDS = 30


def ensure_schema(db: str | Path) -> None:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS study_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started REAL NOT NULL,
                ended REAL,
                goal TEXT NOT NULL,
                task TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS behavior_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                application TEXT,
                category TEXT NOT NULL,
                session_id INTEGER,
                FOREIGN KEY(session_id) REFERENCES study_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_behavior_samples_timestamp ON behavior_samples(timestamp);
            CREATE INDEX IF NOT EXISTS idx_behavior_samples_session ON behavior_samples(session_id);
            CREATE TABLE IF NOT EXISTS study_checkins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                session_id INTEGER NOT NULL,
                focus_rating INTEGER NOT NULL CHECK(focus_rating BETWEEN 1 AND 5),
                progress TEXT NOT NULL CHECK(progress IN ('none', 'partial', 'complete')),
                outcome TEXT NOT NULL,
                FOREIGN KEY(session_id) REFERENCES study_sessions(id)
            );
            CREATE INDEX IF NOT EXISTS idx_study_checkins_session ON study_checkins(session_id, timestamp);
            """
        )
        conn.commit()


def idle_seconds() -> float | None:
    """Seconds since keyboard or mouse input on Windows; None elsewhere."""
    try:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.wintypes.UINT), ("dwTime", ctypes.wintypes.DWORD)]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now = ctypes.windll.kernel32.GetTickCount()
        return max(0.0, ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0)
    except (AttributeError, OSError):
        return None


def classify(app: str | None, title: str | None, settings: dict, idle: float | None) -> str:
    if idle is not None and idle >= 90:
        return "away"
    app = (app or "").lower()
    title = (title or "").lower()
    if not app and not title:
        return "unknown"
    distraction = [x.strip().lower() for x in settings.get("distraction_apps", []) if x.strip()]
    focus = [x.strip().lower() for x in settings.get("focus_apps", []) if x.strip()]
    if any(x in app for x in distraction):
        return "distraction"
    if any(x in app for x in focus):
        return "focus"
    # A task or goal word in the window title is a weak signal, not proof.
    words = [w.lower() for w in (settings.get("goal", "") + " " + settings.get("task", "")).split() if len(w) >= 5]
    if words and any(word in title for word in words):
        return "possible_focus"
    return "unknown"


def sample(db: str | Path, settings: dict, session_id: int | None) -> dict:
    app, title = _current_window_title()
    idle = idle_seconds()
    category = classify(app, title, settings, idle)
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.execute(
            "INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)",
            (time.time(), app, category, session_id),
        )
        conn.commit()
    return {"application": app or "", "category": category, "idle_seconds": idle}


def begin_session(db: str | Path, goal: str, task: str) -> int:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        cur = conn.execute(
            "INSERT INTO study_sessions(started, goal, task) VALUES (?,?,?)",
            (time.time(), goal, task),
        )
        conn.commit()
        return int(cur.lastrowid)


def end_session(db: str | Path, session_id: int) -> None:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.execute("UPDATE study_sessions SET ended=? WHERE id=? AND ended IS NULL", (time.time(), session_id))
        conn.commit()


def active_session(db: str | Path) -> dict | None:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM study_sessions WHERE ended IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None


def last_session_report(db: str | Path) -> dict | None:
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        session = conn.execute(
            "SELECT * FROM study_sessions WHERE ended IS NOT NULL ORDER BY ended DESC LIMIT 1"
        ).fetchone()
        if not session:
            return None
        samples = conn.execute(
            "SELECT category, COUNT(*) FROM behavior_samples WHERE session_id = ? GROUP BY category", (session["id"],)
        ).fetchall()
        edits = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event_type = 'file_edit' AND timestamp BETWEEN ? AND ?",
            (session["started"], session["ended"]),
        ).fetchone()[0]
    minutes = {key: 0 for key in ("focus", "possible_focus", "distraction", "away", "unknown")}
    for category, count in samples:
        minutes[category] = round(count * SAMPLE_SECONDS / 60, 1)
    return {
        "session": dict(session),
        "sampled_minutes": round(sum(minutes.values()), 1),
        "minutes": minutes,
        "project_file_edits": edits,
    }


def summary(db: str | Path, current_session: dict | None) -> dict:
    now = time.time()
    since = now - 7 * 86400
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT timestamp, application, category, session_id FROM behavior_samples "
            "WHERE timestamp >= ? ORDER BY timestamp DESC", (since,)
        ).fetchall()
        event_counts = conn.execute(
            "SELECT event_type, COUNT(*) FROM events WHERE timestamp BETWEEN ? AND ? "
            "AND event_type IN ('file_edit', 'git_state') GROUP BY event_type",
            (current_session["started"] if current_session else now, now),
        ).fetchall()
    today = dt.datetime.fromtimestamp(now).date()
    grid = [[{"focus": 0, "distraction": 0, "away": 0, "unknown": 0} for _ in range(24)] for _ in range(7)]
    for row in rows:
        moment = dt.datetime.fromtimestamp(row["timestamp"])
        days_ago = (today - moment.date()).days
        if 0 <= days_ago < 7:
            category = row["category"]
            bucket = "unknown" if category == "possible_focus" else category
            grid[6 - days_ago][moment.hour][bucket] += 1
    session_rows = [row for row in rows if current_session and row["session_id"] == current_session["id"]]
    counts = {key: 0 for key in ("focus", "possible_focus", "distraction", "away", "unknown")}
    for row in session_rows:
        counts[row["category"]] += 1
    minutes = {key: round(count * SAMPLE_SECONDS / 60, 1) for key, count in counts.items()}
    recent = session_rows[:10]
    switches = sum(1 for left, right in zip(recent, recent[1:]) if left["application"] != right["application"])
    distraction_streak = 0
    for row in session_rows:
        if row["category"] != "distraction":
            break
        distraction_streak += 1
    events = {key: 0 for key in ("file_edit", "git_state")}
    events.update(event_counts)
    return {
        "session": current_session,
        "minutes": minutes,
        "switches_recent": switches,
        "distraction_streak": distraction_streak,
        "latest_category": rows[0]["category"] if rows and now - rows[0]["timestamp"] < SAMPLE_SECONDS * 3 else "unknown",
        "last_sample": rows[0]["timestamp"] if rows else None,
        "heatmap": grid,
        "day_labels": [(today - dt.timedelta(days=6 - i)).strftime("%a %d") for i in range(7)],
        "sample_seconds": SAMPLE_SECONDS,
        "project_events": events,
    }


def rule_feedback(behavior: dict, settings: dict, monitoring: bool) -> list[str]:
    goal = settings.get("goal", "").strip()
    session = behavior["session"]
    minutes = behavior["minutes"]
    if not goal:
        return ["Observation mode is active. The journal records app use and recurring windows even without a goal."] if monitoring else ["Start monitoring to build an activity journal."]
    if not monitoring:
        return ["Start monitoring to see live activity and collect study patterns."]
    if not session:
        return ["Start a study session when you're ready to work on your goal."]
    total = sum(minutes.values())
    if total < 2:
        return ["Your session has just started. Give it a few minutes before reading the pattern."]
    feedback = []
    if behavior.get("distraction_streak", 0) >= 3:
        feedback.append("The last three or more samples were in an app you marked as distracting. Check whether this serves your current task.")
    if minutes["distraction"] >= 2 and minutes["distraction"] >= minutes["focus"]:
        feedback.append("Apps you marked as distracting are taking more time than your focus apps. Try one short uninterrupted block.")
    elif minutes["focus"] >= 10:
        feedback.append(f"About {minutes['focus']:g} minutes were recorded in apps you marked for focus. Keep the next step small and specific.")
    if minutes["away"] >= 5:
        feedback.append("A break appears in the session. Check whether you're ready to resume or end it.")
    if minutes["unknown"] + minutes["possible_focus"] > minutes["focus"] + minutes["distraction"]:
        feedback.append("Much of this session is unclassified. Add your study and distracting apps for clearer feedback.")
    if behavior["switches_recent"] >= 4:
        feedback.append("Several app changes appeared recently. If they were unplanned, return to the current task.")
    return feedback[:3] or ["No strong pattern yet. Continue your task and check again after a few minutes."]
