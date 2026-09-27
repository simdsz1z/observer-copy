"""Factual, local activity journal. Window titles never leave this module's response."""

from __future__ import annotations

import datetime as dt
import re
import sqlite3
import time
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

import behavior


def _subject(title: str | None) -> str:
    if not title:
        return ""
    title = title.replace("\u200b", "").replace("\u200e", "").replace("\u200f", "")
    if re.search(r"password|secret|api[-_ ]?key", title, flags=re.IGNORECASE):
        return ""
    if title.startswith("Observer AI · Local Dashboard"):
        return ""
    title = re.sub(r"\s+and\s+\d+\s+more pages\b", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+-\s+(?:Personal|InPrivate|Guest)\s+-\s+(?:Microsoft\s+Edge|Google\s+Chrome)$", "", title, flags=re.IGNORECASE)
    title = re.sub(r"\s+-\s+(?:Microsoft\s+Edge|Google\s+Chrome|Mozilla\s+Firefox)$", "", title, flags=re.IGNORECASE)
    title = " ".join(title.split()).strip(" -·")[:110]
    if len(title) < 3 or title.lower() in {"new tab", "desktop", "settings", "start", "search"}:
        return ""
    return title


def build(db: str | Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    cutoff = now - 7 * 86400
    with closing(sqlite3.connect(db, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        samples = conn.execute(
            "SELECT timestamp, application, category FROM behavior_samples "
            "WHERE timestamp >= ? ORDER BY timestamp", (cutoff,)
        ).fetchall()
        visits = conn.execute(
            "SELECT timestamp, application, window_title FROM events WHERE timestamp >= ? "
            "AND event_type = 'app_focus' AND window_title IS NOT NULL ORDER BY timestamp",
            (cutoff,),
        ).fetchall()

    days: dict[str, dict] = {}
    all_apps: Counter[str] = Counter()
    for row in samples:
        day = dt.datetime.fromtimestamp(row["timestamp"]).date().isoformat()
        entry = days.setdefault(day, {"date": day, "samples": 0, "active_samples": 0, "away_samples": 0, "switches": 0, "apps": Counter(), "previous_app": None})
        entry["samples"] += 1
        if row["category"] == "away":
            entry["away_samples"] += 1
            continue
        entry["active_samples"] += 1
        app = row["application"] or "Unknown app"
        entry["apps"][app] += 1
        all_apps[app] += 1
        if entry["previous_app"] and entry["previous_app"] != app:
            entry["switches"] += 1
        entry["previous_app"] = app

    subjects: dict[str, dict] = defaultdict(lambda: {"visits": 0, "days": set(), "last_seen": 0})
    for row in visits:
        if (row["application"] or "").lower() not in {"msedge.exe", "chrome.exe", "firefox.exe", "brave.exe", "opera.exe"}:
            continue
        subject = _subject(row["window_title"])
        if not subject:
            continue
        item = subjects[subject]
        item["visits"] += 1
        item["days"].add(dt.datetime.fromtimestamp(row["timestamp"]).date().isoformat())
        item["last_seen"] = row["timestamp"]

    day_rows = []
    for day in sorted(days, reverse=True):
        entry = days[day]
        top = entry["apps"].most_common(1)
        day_rows.append({
            "date": day,
            "active_minutes": round(entry["active_samples"] * behavior.SAMPLE_SECONDS / 60, 1),
            "away_minutes": round(entry["away_samples"] * behavior.SAMPLE_SECONDS / 60, 1),
            "switches": entry["switches"],
            "top_app": top[0][0] if top else None,
            "top_app_minutes": round(top[0][1] * behavior.SAMPLE_SECONDS / 60, 1) if top else 0,
        })
    recurring = [
        {"subject": name, "visits": item["visits"], "days": len(item["days"]), "last_seen": item["last_seen"]}
        for name, item in subjects.items() if item["visits"] >= 2
    ]
    recurring.sort(key=lambda item: (-item["days"], -item["visits"], -item["last_seen"]))
    today = dt.datetime.fromtimestamp(now).date().isoformat()
    today_row = days.get(today)
    observations = []
    if today_row and today_row["active_samples"]:
        top = today_row["apps"].most_common(1)[0]
        minutes = round(top[1] * behavior.SAMPLE_SECONDS / 60, 1)
        observations.append(f"Today, {top[0]} was the most sampled app ({minutes:g} min).")
        if today_row["switches"] >= 4:
            observations.append(f"The sampled app changed {today_row['switches']} times today.")
    elif all_apps:
        top = all_apps.most_common(1)[0]
        minutes = round(top[1] * behavior.SAMPLE_SECONDS / 60, 1)
        observations.append(f"Over the last 7 days, {top[0]} was the most sampled app ({minutes:g} min).")
    if recurring:
        item = recurring[0]
        observations.append(f"The browser page “{item['subject']}” appeared in {item['visits']} focus events across {item['days']} day{'s' if item['days'] != 1 else ''}.")
    if not observations:
        observations.append("No repeated pattern is clear yet. Keep monitoring to build a journal.")
    return {
        "days": day_rows,
        "top_apps": [{"application": app, "minutes": round(count * behavior.SAMPLE_SECONDS / 60, 1)} for app, count in all_apps.most_common(5)],
        "subjects": recurring[:8],
        "observations": observations,
        "sample_seconds": behavior.SAMPLE_SECONDS,
    }
