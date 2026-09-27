"""Basic summaries of context snapshots."""

from __future__ import annotations

from .models import ContextSnapshot  # noqa: F401


def summarize(snapshot: ContextSnapshot) -> str:
    """Return a one-paragraph natural-language summary of the snapshot."""
    if not snapshot.current_application and not snapshot.current_project:
        return "Observer has no recent activity to summarize."
    bits: list[str] = []
    if snapshot.current_application:
        bits.append(f"The user is in {snapshot.current_application}")
    if snapshot.current_project:
        bits.append(f"working on {snapshot.current_project}")
    if snapshot.likely_activity:
        bits.append(f"and looks like they're {snapshot.likely_activity}")
    if snapshot.confidence:
        bits.append(f"(confidence {snapshot.confidence}%)")
    summary = ", ".join(bits) + "."
    if snapshot.recommended_next_step:
        summary += f" Suggested next: {snapshot.recommended_next_step}."
    return summary


def looks_stuck(snapshot: ContextSnapshot) -> bool:
    """Best-effort: return True if the user looks like they're blocked."""
    return bool(snapshot.possible_blockers) or bool(snapshot.open_loops)
