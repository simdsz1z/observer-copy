"""Standalone, local dashboard for this copy of Observer AI."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import behavior
import journal
import secret_store
from observer_ai.collectors import (
    FilesystemCollector,
    ForegroundCollector,
    GitCollector,
    HeartbeatCollector,
)
from observer_ai.orchestrator import Observer


ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "observer.db"
SETTINGS = DATA / "settings.json"
PAGE = ROOT / "dashboard.html"
SCRIPT = ROOT / "dashboard.js"
COMPANION = ROOT / "companion.py"
MINIMAX_KEY = DATA / "minimax-key.bin"
AI_HISTORY = DATA / "ai-feedback.json"
MINIMAX_MODELS = {"MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.7-highspeed"}
AUTO_COOLDOWN = 15 * 60
AUTO_FAILURE_COOLDOWN = 30 * 60
AUTO_DAILY_LIMIT = 12
AUTO_SAMPLE_COUNT = 10


class DashboardState:
    def __init__(self) -> None:
        DATA.mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.observer: Observer | None = None
        self.sample_stop = threading.Event()
        self.sample_thread: threading.Thread | None = None
        self.feedback_thread: threading.Thread | None = None
        self.feedback_lock = threading.Lock()
        self.companion_process: subprocess.Popen | None = None
        self.settings = {"watched_folder": "", "goal": "", "task": "", "focus_apps": [], "distraction_apps": [], "ai_provider": "minimax", "minimax_model": "MiniMax-M3", "auto_feedback": True}
        if SETTINGS.exists():
            try:
                saved = json.loads(SETTINGS.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.settings.update({key: saved[key] for key in self.settings if key in saved})
            except (OSError, ValueError, TypeError):
                pass
        self.ai_history = {"latest": None, "last_attempt": 0, "last_success": 0, "last_session_id": None, "last_sample_id": 0, "day": "", "calls_today": 0, "last_error": ""}
        if AI_HISTORY.exists():
            try:
                saved = json.loads(AI_HISTORY.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    self.ai_history.update({key: saved[key] for key in self.ai_history if key in saved})
            except (OSError, ValueError, TypeError):
                pass
        # Create the database and schema before the first dashboard read.
        from observer_ai.database import connect

        conn = connect(DB)
        conn.close()
        behavior.ensure_schema(DB)

    def save_settings(self) -> None:
        SETTINGS.write_text(json.dumps(self.settings, indent=2, ensure_ascii=False), encoding="utf-8")

    def save_ai_history(self) -> None:
        temporary = AI_HISTORY.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.ai_history, indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(AI_HISTORY)

    def set_folder(self, folder: str) -> None:
        folder = folder.strip()
        if folder:
            path = Path(folder).expanduser().resolve()
            if not path.is_dir():
                raise ValueError("Choose an existing folder.")
            if path == ROOT or path in ROOT.parents or ROOT in path.parents:
                raise ValueError("Choose a folder outside the Observer copy to avoid recording its own data files.")
            folder = str(path)
        with self.lock:
            if self.observer and self.observer.is_monitoring():
                raise ValueError("Stop monitoring before changing the watched folder.")
            self.settings["watched_folder"] = folder
            self.save_settings()

    def set_study(self, data: dict) -> None:
        goal = data.get("goal", "")
        task = data.get("task", "")
        focus = data.get("focus_apps", [])
        distraction = data.get("distraction_apps", [])
        if not isinstance(goal, str) or not isinstance(task, str) or len(goal) > 300 or len(task) > 300:
            raise ValueError("Goal and task must be text under 300 characters.")
        if not all(isinstance(items, list) and len(items) <= 30 and all(isinstance(x, str) and len(x) <= 80 for x in items) for items in (focus, distraction)):
            raise ValueError("App lists must contain up to 30 app names each.")
        with self.lock:
            self.settings.update({"goal": goal.strip(), "task": task.strip(), "focus_apps": [x.strip() for x in focus if x.strip()], "distraction_apps": [x.strip() for x in distraction if x.strip()]})
            self.save_settings()

    def set_ai_settings(self, data: dict) -> None:
        provider = data.get("provider")
        model = data.get("model")
        key = data.get("api_key", "")
        automatic = data.get("auto_feedback", self.settings["auto_feedback"])
        if provider not in ("minimax", "local") or model not in MINIMAX_MODELS or not isinstance(key, str) or not isinstance(automatic, bool):
            raise ValueError("Choose a supported AI provider and model.")
        if key:
            secret_store.save(MINIMAX_KEY, key)
        with self.lock:
            self.settings["ai_provider"] = provider
            self.settings["minimax_model"] = model
            self.settings["auto_feedback"] = automatic
            self.save_settings()

    def clear_minimax_key(self) -> None:
        with self.lock:
            secret_store.clear(MINIMAX_KEY)

    def start_session(self) -> None:
        with self.lock:
            if behavior.active_session(DB):
                return
            if not self.settings["goal"]:
                raise ValueError("Save a study goal first.")
            behavior.begin_session(DB, self.settings["goal"], self.settings["task"])

    def end_session(self) -> None:
        with self.lock:
            session = behavior.active_session(DB)
            if session:
                behavior.end_session(DB, session["id"])

    def _sample_loop(self) -> None:
        while not self.sample_stop.is_set():
            try:
                with self.lock:
                    active = bool(self.observer and self.observer.is_monitoring())
                    settings = dict(self.settings)
                    session = behavior.active_session(DB)
                if active:
                    behavior.sample(DB, settings, session["id"] if session else None)
            except (OSError, sqlite3.Error):
                pass
            self.sample_stop.wait(behavior.SAMPLE_SECONDS)

    def start(self) -> None:
        with self.lock:
            if self.observer and self.observer.is_monitoring():
                return
            factories = [ForegroundCollector, HeartbeatCollector]
            if self.settings["watched_folder"]:
                folder = Path(self.settings["watched_folder"])
                if not folder.is_dir():
                    raise ValueError("The watched folder no longer exists. Choose another folder.")
                factories += [partial(GitCollector, cwd=str(folder)), partial(FilesystemCollector, root=str(folder))]
            observer = Observer(db_path=DB, collectors=factories)
            observer.start()
            self.observer = observer
            self.sample_stop.clear()
            self.sample_thread = threading.Thread(target=self._sample_loop, daemon=True, name="BehaviorSampler")
            self.sample_thread.start()
            self.feedback_thread = threading.Thread(target=self._feedback_loop, daemon=True, name="AutomaticFeedback")
            self.feedback_thread.start()

    def stop(self) -> None:
        self.sample_stop.set()
        with self.lock:
            if self.observer and self.observer.is_monitoring():
                self.observer.stop()
        if self.sample_thread and self.sample_thread.is_alive():
            self.sample_thread.join(timeout=2)
        if self.feedback_thread and self.feedback_thread.is_alive():
            self.feedback_thread.join(timeout=2)

    def status(self) -> dict:
        with self.lock:
            active = bool(self.observer and self.observer.is_monitoring())
            return {
                "monitoring": active,
                "watched_folder": self.settings["watched_folder"],
                "collectors": self.observer.collector_status() if active else [],
                "database": str(DB),
                "study": dict(self.settings),
                "has_minimax_key": MINIMAX_KEY.exists(),
                "companion_open": bool(self.companion_process and self.companion_process.poll() is None),
            }

    def open_companion(self, port: int) -> None:
        with self.lock:
            if self.companion_process and self.companion_process.poll() is None:
                return
            executable = Path(sys.executable)
            if sys.platform == "win32":
                pythonw = executable.with_name("pythonw.exe")
                if pythonw.exists():
                    executable = pythonw
            self.companion_process = subprocess.Popen(
                [str(executable), str(COMPANION), "--port", str(port)],
                cwd=ROOT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

    def _auto_feedback_status(self, session: dict | None = None) -> dict:
        with self.lock:
            settings = dict(self.settings)
            monitoring = bool(self.observer and self.observer.is_monitoring())
            history = dict(self.ai_history)
        session = session if session is not None else behavior.active_session(DB)
        if not settings["auto_feedback"]:
            return {"state": "off", "message": "Automatic feedback is off."}
        if not monitoring:
            return {"state": "waiting", "message": "Start monitoring for automatic feedback."}
        if not settings["goal"] or not session:
            return {"state": "waiting", "message": "Set a goal and start a study session."}
        if settings["ai_provider"] == "minimax" and not MINIMAX_KEY.exists():
            return {"state": "waiting", "message": "Save a MiniMax key to enable automatic feedback."}
        now = time.time()
        with closing(sqlite3.connect(DB, timeout=5)) as conn:
            total, newest, recent = conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0), "
                "SUM(CASE WHEN timestamp >= ? AND category != 'away' THEN 1 ELSE 0 END) "
                "FROM behavior_samples WHERE session_id = ? AND category != 'away'",
                (now - 5 * 60, session["id"]),
            ).fetchone()
            latest_time = conn.execute(
                "SELECT MAX(timestamp) FROM behavior_samples WHERE session_id = ?", (session["id"],)
            ).fetchone()[0]
            last_id = history["last_sample_id"] if history["last_session_id"] == session["id"] else 0
            new_count = conn.execute(
                "SELECT COUNT(*) FROM behavior_samples WHERE session_id = ? AND id > ? AND category != 'away'",
                (session["id"], last_id),
            ).fetchone()[0]
        if not latest_time or now - latest_time > behavior.SAMPLE_SECONDS * 3 or recent < 4:
            return {"state": "waiting", "message": "Waiting for recent study activity."}
        if total < AUTO_SAMPLE_COUNT or new_count < AUTO_SAMPLE_COUNT:
            return {"state": "collecting", "message": "Collecting about 5 minutes of new activity."}
        today = dt.date.today().isoformat()
        if history["day"] == today and history["calls_today"] >= AUTO_DAILY_LIMIT:
            return {"state": "limit", "message": "Today's automatic feedback limit is reached."}
        if history["last_session_id"] == session["id"]:
            cooldown = AUTO_FAILURE_COOLDOWN if history["last_error"] else AUTO_COOLDOWN
            until = history["last_attempt"] + cooldown
            if now < until:
                return {"state": "cooldown", "message": "Next automatic check after the cooldown.", "next_at": until}
        return {"state": "ready", "message": "Automatic feedback is ready.", "sample_id": newest}

    def _feedback_loop(self) -> None:
        while not self.sample_stop.wait(30):
            try:
                self.ai_feedback(automatic=True)
            except (OSError, sqlite3.Error):
                pass

    def ai_feedback(self, automatic: bool = False) -> dict:
        if automatic and self._auto_feedback_status()["state"] != "ready":
            return {"source": "rules", "feedback": []}
        if not self.feedback_lock.acquire(blocking=not automatic):
            return {"source": "rules", "feedback": [], "note": "Feedback is already being generated."}
        try:
            return self._request_ai_feedback(automatic)
        finally:
            self.feedback_lock.release()

    def _request_ai_feedback(self, automatic: bool) -> dict:
        if automatic and self._auto_feedback_status()["state"] != "ready":
            return {"source": "rules", "feedback": []}
        with self.lock:
            settings = dict(self.settings)
            active = bool(self.observer and self.observer.is_monitoring())
        session = behavior.active_session(DB)
        summary = behavior.summary(DB, session)
        fallback = behavior.rule_feedback(summary, settings, active)
        if not summary["session"] or sum(summary["minutes"].values()) < 2:
            return {"source": "rules", "feedback": fallback, "note": "Study feedback needs an active session and a few minutes of samples."}
        # Both providers receive aggregate evidence only; no titles, paths, or app names.
        provider = settings.get("ai_provider", "minimax")
        model = settings.get("minimax_model", "MiniMax-M3")
        prompt = json.dumps({"goal": settings["goal"], "task": settings["task"], "session_minutes_by_category": summary["minutes"], "recent_app_switches": summary["switches_recent"], "current_category": summary["latest_category"], "recent_distraction_samples": summary["distraction_streak"], "watched_project_event_counts": summary["project_events"], "sampling_interval_seconds": behavior.SAMPLE_SECONDS})
        messages = [
            {"role": "system", "content": "You are an automatic, evidence-based activity analyst. Use the goal, task, sampled app categories, recent switching, and watched-project event counts to assess whether observed behavior appears aligned with the task. Give 1-2 specific observations, a cautious inference with uncertainty, and one practical next action. File edits show activity, not completion or quality. App categories show what was open, not thoughts or intent. Never ask the user to rate focus, answer a check-in, or report progress. Do not flatter, moralize, invent motives or mood, or assert verified productivity."},
            {"role": "user", "content": prompt},
        ]
        try:
            if provider == "minimax":
                key = secret_store.load(MINIMAX_KEY)
                if not key:
                    return {"source": "rules", "feedback": fallback, "note": "Add a MiniMax API key in AI settings to get model feedback."}
                payload = {"model": model, "temperature": 0.4, "max_completion_tokens": 1024, "reasoning_split": True, "messages": messages}
                endpoint = "https://api.minimax.io/v1/chat/completions"
                headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
                timeout = 45
            else:
                with urllib.request.urlopen("http://127.0.0.1:1234/v1/models", timeout=2) as response:
                    models = json.load(response).get("data", [])
                if not models:
                    raise ValueError("No local model is loaded.")
                model = models[0]["id"]
                payload = {"model": model, "temperature": 0.4, "max_tokens": 180, "messages": messages}
                endpoint = "http://127.0.0.1:1234/v1/chat/completions"
                headers = {"Content-Type": "application/json"}
                timeout = 20
            with closing(sqlite3.connect(DB, timeout=5)) as conn:
                newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM behavior_samples WHERE session_id = ?", (session["id"],)).fetchone()[0]
            now = time.time()
            today = dt.date.today().isoformat()
            with self.lock:
                if self.ai_history["day"] != today:
                    self.ai_history["day"] = today
                    self.ai_history["calls_today"] = 0
                self.ai_history.update({"last_attempt": now, "last_session_id": session["id"], "last_sample_id": newest, "last_error": ""})
                self.ai_history["calls_today"] += 1
                self.save_ai_history()
            request = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            message = result["choices"][0]["message"]["content"].strip()
            message = re.sub(r"^\s*<think>.*?</think>\s*", "", message, flags=re.DOTALL)
            if not message:
                raise ValueError("The model returned no feedback.")
            result = {"source": "minimax" if provider == "minimax" else "local_ai", "model": model, "feedback": message, "timestamp": time.time(), "session_id": session["id"], "automatic": automatic}
            with self.lock:
                self.ai_history["latest"] = result
                self.ai_history["last_success"] = result["timestamp"]
                self.save_ai_history()
            return result
        except urllib.error.HTTPError as exc:
            note = f"MiniMax request failed (HTTP {exc.code}). Check the API key and model."
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, IndexError, TypeError, OSError):
            note = "MiniMax is unavailable. Check your connection and API key." if provider == "minimax" else "Local AI unavailable. Start LM Studio on port 1234."
        with self.lock:
            self.ai_history["last_error"] = note
            self.save_ai_history()
        return {"source": "rules", "feedback": fallback, "note": note}

    def overview(self) -> dict:
        cutoff = time.time() - 86400
        with closing(sqlite3.connect(DB, timeout=5)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT timestamp, source, event_type, application, window_title, project, "
                "project_path, metadata_json FROM events ORDER BY timestamp DESC LIMIT 80"
            ).fetchall()
            recent_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp >= ? AND source != 'heartbeat'", (cutoff,)
            ).fetchone()[0]
            app_count = conn.execute(
                "SELECT COUNT(DISTINCT application) FROM events WHERE timestamp >= ? AND application IS NOT NULL", (cutoff,)
            ).fetchone()[0]
            project_count = conn.execute(
                "SELECT COUNT(DISTINCT project) FROM events WHERE timestamp >= ? AND project IS NOT NULL", (cutoff,)
            ).fetchone()[0]
            app_rows = conn.execute(
                "SELECT application, COUNT(*) AS n FROM events "
                "WHERE timestamp >= ? AND event_type = 'app_focus' AND application IS NOT NULL "
                "GROUP BY application ORDER BY n DESC LIMIT 6", (cutoff,)
            ).fetchall()
            project_rows = conn.execute(
                "SELECT project, COUNT(*) AS n FROM events "
                "WHERE timestamp >= ? AND project IS NOT NULL "
                "GROUP BY project ORDER BY n DESC LIMIT 6", (cutoff,)
            ).fetchall()
            hourly_rows = conn.execute(
                "SELECT CAST(timestamp / 3600 AS INTEGER) AS hour, COUNT(*) AS n FROM events "
                "WHERE timestamp >= ? AND source != 'heartbeat' GROUP BY hour", (cutoff,)
            ).fetchall()
            latest = conn.execute(
                "SELECT timestamp, current_application, current_project, current_branch, "
                "likely_activity, confidence, recommended_next_step FROM snapshots "
                "ORDER BY timestamp DESC LIMIT 1"
            ).fetchone()
        events = []
        for row in rows:
            if row["source"] == "heartbeat":
                continue
            item = {key: row[key] for key in row.keys() if key != "metadata_json"}
            try:
                item["metadata"] = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
            events.append(item)
        hours = {row["hour"]: row["n"] for row in hourly_rows}
        current_hour = int(time.time() // 3600)
        current = dict(latest) if latest else None
        with self.lock:
            if self.observer and self.observer.is_monitoring():
                live = self.observer.snapshot()
                current = {
                    "timestamp": time.time(),
                    "current_application": live.current_application,
                    "current_project": live.current_project,
                    "current_branch": live.current_branch,
                    "likely_activity": live.likely_activity,
                    "confidence": live.confidence,
                    "recommended_next_step": live.recommended_next_step,
                }
        status = self.status()
        behavior_summary = behavior.summary(DB, behavior.active_session(DB))
        activity_journal = journal.build(DB)
        with self.lock:
            latest_ai = self.ai_history["latest"]
            last_error = self.ai_history["last_error"]
        if not behavior_summary["session"] or not latest_ai or latest_ai.get("session_id") != behavior_summary["session"]["id"]:
            latest_ai = None
        return {
            "status": status,
            "current": current,
            "events": events[:50],
            "stats": {
                "activity_events_24h": recent_count,
                "applications_24h": app_count,
                "projects_24h": project_count,
            },
            "apps": [dict(row) for row in app_rows],
            "projects": [dict(row) for row in project_rows],
            "hours": [{"timestamp": (current_hour - i) * 3600, "count": hours.get(current_hour - i, 0)} for i in range(23, -1, -1)],
            "behavior": behavior_summary,
            "last_session_report": behavior.last_session_report(DB),
            "journal": activity_journal,
            "feedback": behavior.rule_feedback(behavior_summary, status["study"], status["monitoring"]),
            "ai_feedback": latest_ai,
            "auto_feedback": {**self._auto_feedback_status(behavior_summary["session"]), "last_error": last_error},
        }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: DashboardState):
        self.state = state
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj: dict) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].lower()
        return host in ("127.0.0.1", "localhost")

    def do_GET(self) -> None:
        if not self._local_request():
            self._json(403, {"error": "Local requests only."})
            return
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/dashboard.js":
            self._send(200, SCRIPT.read_bytes(), "text/javascript; charset=utf-8")
        elif path == "/api/overview":
            self._json(200, self.server.state.overview())
        elif path == "/api/companion":
            overview = self.server.state.overview()
            self._json(200, {key: overview[key] for key in ("status", "current", "behavior", "journal", "feedback", "ai_feedback", "auto_feedback")})
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        if not self._local_request():
            self._json(403, {"error": "Local requests only."})
            return
        origin = self.headers.get("Origin", "")
        if origin and origin not in (f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"):
            self._json(403, {"error": "Invalid origin."})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 8192 or size < 0:
                raise ValueError("Request too large.")
            data = json.loads(self.rfile.read(size) or b"{}")
            if not isinstance(data, dict):
                raise ValueError("Invalid request.")
            path = urlparse(self.path).path
            if path == "/api/start":
                self.server.state.start()
            elif path == "/api/stop":
                self.server.state.stop()
            elif path == "/api/folder":
                if not isinstance(data.get("folder"), str):
                    raise ValueError("Folder must be text.")
                self.server.state.set_folder(data["folder"])
            elif path == "/api/study":
                self.server.state.set_study(data)
            elif path == "/api/ai/settings":
                self.server.state.set_ai_settings(data)
            elif path == "/api/ai/key/clear":
                self.server.state.clear_minimax_key()
            elif path == "/api/session/start":
                self.server.state.start_session()
            elif path == "/api/session/end":
                self.server.state.end_session()
            elif path == "/api/companion/open":
                self.server.state.open_companion(self.server.server_port)
            elif path == "/api/feedback":
                self._json(200, self.server.state.ai_feedback())
                return
            else:
                self._json(404, {"error": "Not found."})
                return
            self._json(200, self.server.state.status())
        except (ValueError, OSError) as exc:
            self._json(400, {"error": str(exc)})


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Observer AI dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    state = DashboardState()
    server = DashboardServer(("127.0.0.1", args.port), state)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Observer dashboard: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()


if __name__ == "__main__":
    main()
