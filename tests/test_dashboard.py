import json
import io
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path
from unittest import mock

import behavior
import dashboard
import journal


class BehaviorTests(unittest.TestCase):
    def test_categories_use_explicit_apps_and_idle_state(self):
        settings = {"goal": "Study history", "task": "Review chapter", "focus_apps": ["Word.exe"], "distraction_apps": ["Game.exe"]}
        self.assertEqual(behavior.classify("Word.exe", "Notes", settings, 0), "focus")
        self.assertEqual(behavior.classify("Game.exe", "Review chapter", settings, 0), "distraction")
        self.assertEqual(behavior.classify("Browser.exe", "Something else", settings, 0), "unknown")
        self.assertEqual(behavior.classify("Word.exe", "Notes", settings, 100), "away")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        data = Path(self.temp.name) / "data"
        self.patches = [
            mock.patch.object(dashboard, "DATA", data),
            mock.patch.object(dashboard, "DB", data / "observer.db"),
            mock.patch.object(dashboard, "SETTINGS", data / "settings.json"),
            mock.patch.object(dashboard, "MINIMAX_KEY", data / "minimax-key.bin"),
            mock.patch.object(dashboard, "AI_HISTORY", data / "ai-feedback.json"),
        ]
        for patch in self.patches:
            patch.start()
        self.state = dashboard.DashboardState()
        self.server = dashboard.DashboardServer(("127.0.0.1", 0), self.state)
        self.server.daemon_threads = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.state.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def request(self, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def test_study_session_monitoring_and_feedback(self):
        self.assertEqual(self.request("/api/study", {"goal": "Review biology", "task": "Chapter 2", "focus_apps": ["Word.exe"], "distraction_apps": ["Game.exe"]})[0], 200)
        self.assertEqual(self.request("/api/session/start", {})[0], 200)
        self.assertEqual(self.request("/api/start", {})[1]["monitoring"], True)
        overview = self.request("/api/overview")[1]
        self.assertEqual(overview["status"]["study"]["goal"], "Review biology")
        self.assertIsNotNone(overview["behavior"]["session"])
        self.assertEqual(len(overview["behavior"]["heatmap"]), 7)
        self.assertEqual(len(overview["behavior"]["heatmap"][0]), 24)
        with mock.patch.object(dashboard.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            feedback = self.state.ai_feedback()
        self.assertEqual(feedback["source"], "rules")
        self.assertEqual(self.request("/api/stop", {})[1]["monitoring"], False)
        self.assertEqual(self.request("/api/session/end", {})[0], 200)
        self.assertIsNone(self.request("/api/overview")[1]["behavior"]["session"])

    def test_local_ai_receives_only_aggregate_session_data(self):
        self.state.set_ai_settings({"provider": "local", "model": "MiniMax-M3", "api_key": ""})
        self.state.set_study({"goal": "Review biology", "task": "Chapter 2", "focus_apps": ["Word.exe"], "distraction_apps": []})
        self.state.start_session()
        session = behavior.active_session(dashboard.DB)
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany(
                "INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)",
                [(time.time(), "Word.exe", "focus", session["id"])] * 4,
            )
            conn.commit()
        sent = []

        def fake_open(request, timeout):
            if isinstance(request, str):
                self.assertEqual(request, "http://127.0.0.1:1234/v1/models")
                return io.BytesIO(b'{"data":[{"id":"local-model"}]}')
            self.assertEqual(request.full_url, "http://127.0.0.1:1234/v1/chat/completions")
            sent.append(json.loads(request.data))
            return io.BytesIO(b'{"choices":[{"message":{"content":"Try a short review block."}}]}')

        with mock.patch.object(dashboard.urllib.request, "urlopen", side_effect=fake_open):
            result = self.state.ai_feedback()
        self.assertEqual(result["source"], "local_ai")
        prompt = sent[0]["messages"][1]["content"]
        self.assertIn("session_minutes_by_category", prompt)
        self.assertNotIn("Word.exe", prompt)

    def test_minimax_key_is_encrypted_redacted_and_used_for_feedback(self):
        dummy_key = "dummy-minimax-key"
        status = self.request("/api/ai/settings", {"provider": "minimax", "model": "MiniMax-M3", "api_key": dummy_key})[1]
        self.assertTrue(status["has_minimax_key"])
        self.assertNotIn(dummy_key, json.dumps(status))
        self.assertNotIn(dummy_key, dashboard.SETTINGS.read_text(encoding="utf-8"))
        self.assertNotIn(dummy_key.encode(), dashboard.MINIMAX_KEY.read_bytes())
        self.state.set_study({"goal": "Review biology", "task": "Chapter 2", "focus_apps": [], "distraction_apps": []})
        self.state.start_session()
        session = behavior.active_session(dashboard.DB)
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany(
                "INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)",
                [(time.time(), "Word.exe", "unknown", session["id"])] * 4,
            )
            conn.commit()

        def fake_open(request, timeout):
            self.assertEqual(request.full_url, "https://api.minimax.io/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), f"Bearer {dummy_key}")
            body = json.loads(request.data)
            self.assertEqual(body["model"], "MiniMax-M3")
            self.assertNotIn("Word.exe", json.dumps(body))
            return io.BytesIO(b'{"choices":[{"message":{"content":"<think>internal</think>Try a short study block."}}]}')

        with mock.patch.object(dashboard.urllib.request, "urlopen", side_effect=fake_open):
            feedback = self.state.ai_feedback()
        self.assertEqual(feedback["source"], "minimax")
        self.assertEqual(feedback["feedback"], "Try a short study block.")
        cleared = self.request("/api/ai/key/clear", {})[1]
        self.assertFalse(cleared["has_minimax_key"])

    def test_automatic_feedback_waits_for_fresh_samples_and_respects_limits(self):
        self.state.set_ai_settings({"provider": "minimax", "model": "MiniMax-M3", "api_key": "dummy-key", "auto_feedback": True})
        self.state.set_study({"goal": "Finish a report", "task": "Review notes", "focus_apps": [], "distraction_apps": []})
        self.state.start_session()
        session = behavior.active_session(dashboard.DB)
        self.state.observer = mock.Mock()
        self.state.observer.is_monitoring.return_value = True
        self.assertEqual(self.state._auto_feedback_status()["state"], "waiting")
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany("INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)", [(time.time(), "Browser.exe", "unknown", session["id"])] * 10)
            conn.commit()
        self.assertEqual(self.state._auto_feedback_status()["state"], "ready")
        with mock.patch.object(dashboard.urllib.request, "urlopen", return_value=io.BytesIO(b'{"choices":[{"message":{"content":"Review one page now."}}]}')) as call:
            result = self.state.ai_feedback(automatic=True)
            self.assertEqual(result["source"], "minimax")
            self.assertTrue(result["automatic"])
            self.state.ai_feedback(automatic=True)
            self.assertEqual(call.call_count, 1)
        self.assertEqual(self.state.ai_history["latest"]["feedback"], "Review one page now.")
        self.assertEqual(self.state.ai_history["calls_today"], 1)
        self.assertEqual(self.state._auto_feedback_status()["state"], "collecting")
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany("INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)", [(time.time(), "Browser.exe", "unknown", session["id"])] * 10)
            conn.commit()
        self.assertEqual(self.state._auto_feedback_status()["state"], "cooldown")
        self.state.ai_history["calls_today"] = dashboard.AUTO_DAILY_LIMIT
        self.assertEqual(self.state._auto_feedback_status()["state"], "limit")
        self.state.observer = None

    def test_goal_free_journal_reports_observed_activity(self):
        now = time.time()
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany(
                "INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,NULL)",
                [(now - 90, "Browser.exe", "unknown"), (now - 60, "Browser.exe", "unknown"), (now - 30, "Notes.exe", "unknown")],
            )
            conn.executemany(
                "INSERT INTO events(timestamp, source, event_type, application, window_title) VALUES (?,'foreground','app_focus','msedge.exe',?)",
                [(now - 80, "Project outline and 42 more pages - Personal - Microsoft Edge"), (now - 20, "Project outline and 43 more pages - Personal - Microsoft Edge")],
            )
            conn.commit()
        report = journal.build(dashboard.DB, now)
        self.assertEqual(report["days"][0]["active_minutes"], 1.5)
        self.assertEqual(report["days"][0]["switches"], 1)
        self.assertEqual(report["subjects"][0]["subject"], "Project outline")
        self.assertEqual(report["subjects"][0]["visits"], 2)
        overview = self.request("/api/overview")[1]
        self.assertFalse(overview["status"]["study"]["goal"])
        self.assertEqual(overview["journal"]["subjects"][0]["subject"], "Project outline")
        self.assertIn("journal", overview["feedback"][0].lower())


if __name__ == "__main__":
    unittest.main()
