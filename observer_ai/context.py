"""ContextEngine: roll raw ActivityEvents into a ContextSnapshot."""

from __future__ import annotations

from collections import deque
from typing import Iterable

from .models import (
    ActivityEvent,
    ContextSnapshot,
    EventType,
    OpenLoop,
    ProjectState,
)


_ACTIVITY_HINTS = {
    "windows terminal": "running shell commands",
    "powershell": "running shell commands",
    "cmd": "running shell commands",
    "bash": "running shell commands",
    "code": "editing code",
    "visual studio code": "editing code",
    "pycharm": "editing code",
    "intellij": "editing code",
    "chrome": "browsing the web",
    "firefox": "browsing the web",
    "edge": "browsing the web",
    "safari": "browsing the web",
    "explorer": "browsing files",
    "notion": "writing notes",
    "obsidian": "writing notes",
    "figma": "designing",
    "photoshop": "designing",
    "word": "writing",
    "notepad": "writing",
    "slack": "communicating",
    "discord": "communicating",
    "telegram": "communicating",
    "wechat": "communicating",
}


def infer_activity_kind(
    current_application: str | None,
    current_window_title: str | None,
    recent_events: Iterable[ActivityEvent] = (),
) -> str | None:
    """Best-effort guess of what the user is currently doing."""
    if not current_application:
        return None
    app = current_application.lower()
    for needle, activity in _ACTIVITY_HINTS.items():
        if needle in app:
            return activity
    if current_window_title and " - " in current_window_title:
        return "editing a file"
    return None


class ContextEngine:
    """Maintain a rolling window of events and produce a ContextSnapshot.

    The engine is in-memory only — collectors feed events via ``update()``,
    and ``snapshot()`` rolls them up for local consumers.
    """

    def __init__(self, max_events: int = 256, max_projects: int = 16) -> None:
        self._events: deque[ActivityEvent] = deque(maxlen=max_events)
        self._projects: dict[str, ProjectState] = {}
        self._max_projects = max_projects

    # -- ingest ---------------------------------------------------------

    def update(self, event: ActivityEvent) -> None:
        if not isinstance(event, ActivityEvent):
            raise TypeError(f"expected ActivityEvent, got {type(event).__name__}")
        self._events.append(event)
        if event.project:
            self._record_project(event)

    def _record_project(self, event: ActivityEvent) -> None:
        state = self._projects.get(event.project)
        if state is None:
            state = ProjectState(name=event.project, path=event.project_path)
            self._projects[event.project] = state
            if len(self._projects) > self._max_projects:
                # drop the least-recently-touched project
                victim = min(self._projects.values(), key=lambda p: p.last_seen)
                self._projects.pop(victim.name, None)
        state.last_seen = event.timestamp
        if event.project_path and not state.path:
            state.path = event.project_path
        if event.event_type == EventType.GIT_STATE:
            meta = event.metadata or {}
            if meta.get("branch"):
                state.branch = meta["branch"]
            if "has_uncommitted" in meta:
                state.has_uncommitted = bool(meta["has_uncommitted"])

    # -- query ----------------------------------------------------------

    def snapshot(self) -> ContextSnapshot:
        events = list(self._events)
        if not events:
            return ContextSnapshot()

        # Most recent APP_FOCUS event drives current_application etc.
        current_app: str | None = None
        current_title: str | None = None
        current_project: str | None = None
        current_project_path: str | None = None
        current_branch: str | None = None

        # Walk events in reverse for "current" state.
        for event in reversed(events):
            if event.event_type == EventType.APP_FOCUS and current_app is None:
                current_app = event.application
                current_title = event.window_title
                current_project = event.project or current_project
                current_project_path = event.project_path or current_project_path
            if event.event_type == EventType.GIT_STATE and current_branch is None:
                current_branch = (event.metadata or {}).get("branch")

        # Progress = last 3 distinct apps or projects, in chronological order.
        recent_progress: list[str] = []
        for event in events[-12:]:
            label: str | None = None
            if event.event_type == EventType.APP_FOCUS and event.application:
                label = f"used {event.application}"
            elif event.event_type == EventType.GIT_STATE and event.project:
                branch = (event.metadata or {}).get("branch", "?")
                label = f"touched {event.project} ({branch})"
            elif event.event_type == EventType.FILE_EDIT and event.metadata.get("path"):
                label = f"edited {event.metadata['path']}"
            if label and (not recent_progress or recent_progress[-1] != label):
                recent_progress.append(label)
        recent_progress = recent_progress[-5:]

        # Blockers = the most recent git event flagged has_uncommitted.
        blockers: list[str] = []
        for event in reversed(events):
            if event.event_type == EventType.GIT_STATE:
                meta = event.metadata or {}
                if meta.get("has_uncommitted"):
                    changed = meta.get("changed_files") or []
                    if changed:
                        blockers.append("uncommitted: " + ", ".join(changed[:3]))
                    else:
                        blockers.append("uncommitted changes")
                break

        # Open loops: any project with has_uncommitted == True is an open loop.
        open_loops: list[OpenLoop] = []
        for project in self._projects.values():
            if project.has_uncommitted:
                confidence = 80 if project.name == current_project else 50
                summary = f"{project.name} has uncommitted changes"
                if project.branch:
                    summary += f" on {project.branch}"
                open_loops.append(OpenLoop(confidence=confidence, summary=summary))
        # Most recently seen loop first.
        open_loops.sort(key=lambda loop: -loop.confidence)
        open_loops = open_loops[:5]

        likely_activity = infer_activity_kind(current_app, current_title, events)
        confidence = 70 if likely_activity else 30

        recommended: str | None = None
        if current_project and current_project_path:
            if open_loops:
                recommended = f"finish edits in {current_project}"
            else:
                recommended = f"continue work in {current_project}"

        recent_projects = sorted(
            self._projects.values(),
            key=lambda p: p.last_seen,
            reverse=True,
        )[: self._max_projects]

        return ContextSnapshot(
            current_application=current_app,
            current_window_title=current_title,
            current_project=current_project,
            current_project_path=current_project_path,
            current_branch=current_branch,
            likely_activity=likely_activity,
            confidence=confidence,
            recent_progress=recent_progress,
            possible_blockers=blockers,
            open_loops=open_loops,
            recommended_next_step=recommended,
            recent_projects=recent_projects,
        )

    # -- debug ----------------------------------------------------------

    def events(self) -> list[ActivityEvent]:
        return list(self._events)
