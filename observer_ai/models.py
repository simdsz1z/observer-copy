"""Activity + context models for Observer AI."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """The kinds of activity events Observer AI can ingest."""

    APP_FOCUS = "app_focus"
    GIT_STATE = "git_state"
    FILE_EDIT = "file_edit"
    COMMAND_RUN = "command_run"
    CLIPBOARD = "clipboard"
    SCREENSHOT = "screenshot"


@dataclass
class ActivityEvent:
    """A single observation from a collector."""

    timestamp: float
    source: str  # "foreground" | "git" | "filesystem" | "screenshot" | ...
    event_type: EventType
    application: str | None = None
    window_title: str | None = None
    project: str | None = None
    project_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class OpenLoop:
    """A thing that looks like a pending commitment."""

    confidence: int  # 0..100
    summary: str


@dataclass
class ProjectState:
    """One project the user has touched recently."""

    name: str
    path: str | None = None
    branch: str | None = None
    has_uncommitted: bool = False
    last_seen: float = 0.0


@dataclass
class ContextSnapshot:
    """A roll-up of everything Observer AI currently knows."""

    current_application: str | None = None
    current_window_title: str | None = None
    current_project: str | None = None
    current_project_path: str | None = None
    current_branch: str | None = None
    likely_activity: str | None = None
    confidence: int = 0
    recent_progress: list[str] = field(default_factory=list)
    possible_blockers: list[str] = field(default_factory=list)
    open_loops: list[OpenLoop] = field(default_factory=list)
    recommended_next_step: str | None = None
    recent_projects: list[ProjectState] = field(default_factory=list)

    def to_compact_text(self) -> str:
        """One-line-per-field summary used to feed the LLM."""
        parts: list[str] = []
        if self.current_application:
            parts.append(f"app: {self.current_application}")
        if self.current_project:
            parts.append(f"project: {self.current_project}")
        if self.current_branch:
            parts.append(f"branch: {self.current_branch}")
        if self.likely_activity:
            parts.append(f"activity: {self.likely_activity} ({self.confidence}%)")
        if self.recent_progress:
            parts.append("progress: " + "; ".join(self.recent_progress[:5]))
        if self.possible_blockers:
            parts.append("blockers: " + "; ".join(self.possible_blockers[:3]))
        if self.open_loops:
            parts.append(
                "loops: "
                + "; ".join(f"{loop.confidence}% {loop.summary}" for loop in self.open_loops[:3])
            )
        if self.recommended_next_step:
            parts.append(f"next: {self.recommended_next_step}")
        return " | ".join(parts) or "(no context)"
