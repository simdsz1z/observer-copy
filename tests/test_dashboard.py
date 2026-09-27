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
import activity_context
import dashboard
import journal


class BehaviorTests(unittest.TestCase):
    def test_categories_use_explicit_apps_and_idle_state(self):
        settings = {"goal": "Study history", "task": "Review chapter", "focus_apps": ["Word.exe"], "distraction_apps": ["Game.exe"]}
        self.assertEqual(behavior.classify("Word.exe", "Notes", settings, 0), "focus")
        self.assertEqual(behavior.classify("Game.exe", "Review chapter", settings, 0), "distraction")
        self.assertEqual(behavior.classify("Browser.exe", "Something else", settings, 0), "unknown")
        self.assertEqual(behavior.classify("Word.exe", "Notes", settings, 100), "away")

    def test_browser_title_filter_removes_private_context(self):
        self.assertEqual(behavior.safe_page_title("msedge.exe", "How pull requests work - YouTube"), "How pull requests work - YouTube")
        self.assertIsNone(behavior.safe_page_title("chrome.exe", "Inbox - personal@example.com - Gmail"))
        self.assertIsNone(behavior.safe_page_title("chrome.exe", "My API key - Chrome"))
        self.assertIsNone(behavior.safe_page_title("code.exe", "Private project"))


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
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.execute("INSERT INTO study_checkins(timestamp, session_id, focus_rating, progress, outcome) VALUES (?,?,?,?,?)", (time.time(), session["id"], 4, "partial", "Private result"))
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
        self.assertNotIn("focus_rating", prompt)
        self.assertNotIn("Private result", prompt)
        self.assertIn("watched_project_event_counts", prompt)

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
        self.assertEqual(feedback["feedback"], "AI feedback could not be read reliably. Observer will retry when there is new activity.")
        cleared = self.request("/api/ai/key/clear", {})[1]
        self.assertFalse(cleared["has_minimax_key"])

    def test_ai_uses_video_subject_and_classifies_each_context(self):
        self.state.set_ai_settings({"provider": "minimax", "model": "MiniMax-M3", "api_key": "dummy-key"})
        self.state.set_study({"goal": "Develop a program", "task": "Learn pull requests", "focus_apps": [], "distraction_apps": []})
        self.state.start_session()
        session = behavior.active_session(dashboard.DB)
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.executemany(
                "INSERT INTO behavior_samples(timestamp, application, category, session_id, window_title) VALUES (?,?,?,?,?)",
                [(time.time(), "msedge.exe", "unknown", session["id"], title) for title in
                 ["How pull requests work - YouTube"] * 2 + ["Funny cat compilation - YouTube"] * 2],
            )
            conn.commit()
        contexts = activity_context.build(dashboard.DB, session)["contexts"]
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["page_title"], "How pull requests work - YouTube")
        def fake_open(request, timeout):
            prompt = json.loads(json.loads(request.data)["messages"][1]["content"])
            self.assertIn("How pull requests work - YouTube", str(prompt["recent_contexts"]))
            self.assertIn("Funny cat compilation - YouTube", str(prompt["recent_contexts"]))
            body = {"alignment": "mixed", "confidence": "medium", "observation": "The pull request tutorial relates to your task; the cat video does not.", "next_step": "Return to the pull request tutorial.", "classifications": [{"id": 1, "label": "work_related", "reason": "Pull request tutorial matches the task."}, {"id": 2, "label": "off_task", "reason": "Cat video is unrelated."}]}
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(body)}}]}).encode())
        with mock.patch.object(dashboard.urllib.request, "urlopen", side_effect=fake_open):
            result = self.state.ai_feedback()
        self.assertEqual(result["analysis"]["alignment"], "mixed")
        self.assertEqual([item["label"] for item in result["analysis"]["classifications"]], ["work_related", "off_task"])

    def test_generic_browser_activity_cannot_be_declared_off_task(self):
        contexts = [{"id": 1, "app": "Browser", "page_title": None, "minutes": 18}]
        claim = {"alignment": "off_task", "confidence": "high", "observation": "You were distracted.", "next_step": "Work harder.", "classifications": [{"id": 1, "label": "off_task", "reason": "Browser."}]}
        result = dashboard.parse_analysis(json.dumps(claim), contexts)
        self.assertEqual(result["alignment"], "insufficient_evidence")
        self.assertEqual(result["confidence"], "low")
        self.assertEqual(result["classifications"][0]["label"], "insufficient_context")

    def test_older_samples_use_matching_foreground_page_title(self):
        self.state.set_study({"goal": "Build an app", "task": "Learn pull requests", "focus_apps": [], "distraction_apps": []})
        self.state.start_session()
        session = behavior.active_session(dashboard.DB)
        now = time.time()
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.execute("UPDATE study_sessions SET started = ? WHERE id = ?", (now - 10, session["id"]))
            conn.execute("INSERT INTO events(timestamp, source, event_type, application, window_title) VALUES (?,'foreground','app_focus','msedge.exe',?)", (now - 3, "How pull requests work - YouTube"))
            conn.execute("INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)", (now - 2, "msedge.exe", "unknown", session["id"]))
            conn.commit()
        contexts = activity_context.build(dashboard.DB, behavior.active_session(dashboard.DB), now)["contexts"]
        self.assertEqual(contexts[0]["page_title"], "How pull requests work - YouTube")

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
        self.assertEqual(self.state.ai_history["latest"]["feedback"], "AI feedback could not be read reliably. Observer will retry when there is new activity.")
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

    def test_session_evidence_is_observed_automatically(self):
        self.request("/api/study", {"goal": "Finish a report", "task": "Draft introduction", "focus_apps": [], "distraction_apps": []})
        self.request("/api/session/start", {})
        session = behavior.active_session(dashboard.DB)
        now = time.time()
        with closing(sqlite3.connect(dashboard.DB)) as conn:
            conn.execute("INSERT INTO behavior_samples(timestamp, application, category, session_id) VALUES (?,?,?,?)", (now, "Word.exe", "unknown", session["id"]))
            conn.execute("INSERT INTO events(timestamp, source, event_type) VALUES (?,'filesystem','file_edit')", (now,))
            conn.commit()
        active = self.request("/api/overview")[1]["behavior"]
        self.assertEqual(active["project_events"]["file_edit"], 1)
        self.assertEqual(active["minutes"]["unknown"], 0.5)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/api/checkin", {"focus_rating": 4, "progress": "complete", "outcome": ""})
        self.request("/api/session/end", {})
        overview = self.request("/api/overview")[1]
        self.assertIsNone(overview["behavior"]["session"])
        self.assertEqual(overview["last_session_report"]["project_file_edits"], 1)
        self.assertEqual(overview["last_session_report"]["minutes"]["unknown"], 0.5)


if __name__ == "__main__":
    unittest.main()
