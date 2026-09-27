"""Compact, privacy-filtered activity context for goal-aware AI feedback."""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import behavior


BROWSERS = {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe"}
APP_LABELS = {
    "code.exe": "VS Code", "cursor.exe": "Cursor", "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ", "devenv.exe": "Visual Studio", "windowsterminal.exe": "Terminal",
    "wt.exe": "Terminal", "powershell.exe": "PowerShell", "pwsh.exe": "PowerShell",
    "cmd.exe": "Command Prompt", "winword.exe": "Word", "excel.exe": "Excel",
    "steam.exe": "Steam", "discord.exe": "Discord",
}


def app_label(app: str | None) -> str:
    name = (app or "").lower()
    if name in BROWSERS:
        return "Browser"
    return APP_LABELS.get(name, "Other app" if name else "App unavailable")


def build(db: str | Path, session: dict, now: float | None = None) -> dict:
    """Summarize recent sampled contexts; event titles only fill older samples."""
    now = time.time() if now is None else now
    start = max(session["started"], now - 30 * 60)
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        samples = conn.execute(
            "SELECT timestamp, application, category, window_title FROM behavior_samples "
            "WHERE session_id = ? AND timestamp BETWEEN ? AND ? ORDER BY timestamp",
            (session["id"], start, now),
        ).fetchall()
        seed = conn.execute(
            "SELECT timestamp, application, window_title FROM events WHERE event_type = 'app_focus' "
            "AND timestamp < ? ORDER BY timestamp DESC LIMIT 1", (start,),
        ).fetchone()
        events = conn.execute(
            "SELECT timestamp, application, window_title FROM events WHERE event_type = 'app_focus' "
            "AND timestamp BETWEEN ? AND ? ORDER BY timestamp", (start, now),
        ).fetchall()
    foreground = [seed] if seed else []
    foreground.extend(events)
    position = 0
    current = None
    grouped = defaultdict(lambda: {"samples": 0, "last_seen": 0, "categories": defaultdict(int)})
    for row in samples:
        while position < len(foreground) and foreground[position]["timestamp"] <= row["timestamp"]:
            current = foreground[position]
            position += 1
        app = row["application"]
        title = row["window_title"]
        if not title and current and current["application"] == app:
            title = behavior.safe_page_title(app, current["window_title"])
        title = behavior.safe_page_title(app, title)
        label = app_label(app)
        key = (label, title or "")
        item = grouped[key]
        item["samples"] += 1
        item["last_seen"] = row["timestamp"]
        item["categories"][row["category"]] += 1
    ranked = sorted(grouped.items(), key=lambda pair: (-pair[1]["samples"], -pair[1]["last_seen"]))
    selected = ranked[:8]
    for entry in sorted(ranked, key=lambda pair: -pair[1]["last_seen"])[:4]:
        if entry not in selected:
            selected.append(entry)
    contexts = []
    for index, ((label, title), item) in enumerate(selected[:12], 1):
        contexts.append({
            "id": index,
            "app": label,
            "page_title": title or None,
            "minutes": round(item["samples"] * behavior.SAMPLE_SECONDS / 60, 1),
            "last_seen_seconds_ago": round(now - item["last_seen"]),
            "sample_categories": dict(item["categories"]),
        })
    return {"window_minutes": round(len(samples) * behavior.SAMPLE_SECONDS / 60, 1), "contexts": contexts}
