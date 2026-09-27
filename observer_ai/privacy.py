"""Privacy helpers for filtering events before local storage."""

from __future__ import annotations

from collections.abc import Callable

from .models import ActivityEvent

_block_predicates: list[Callable[[ActivityEvent], bool]] = []


def register_block(predicate: Callable[[ActivityEvent], bool]) -> None:
    """Drop events for which ``predicate(event)`` is True."""
    _block_predicates.append(predicate)


def block_application(*app_names: str) -> None:
    """Convenience: block events from these application names (case-insensitive)."""
    lowered = {a.lower() for a in app_names}

    def predicate(event: ActivityEvent) -> bool:
        return bool(event.application and event.application.lower() in lowered)

    register_block(predicate)


def block_window_title(*patterns: str) -> None:
    """Block events whose window title matches any of the given substrings."""
    import re

    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    def predicate(event: ActivityEvent) -> bool:
        if not event.window_title:
            return False
        return any(p.search(event.window_title) for p in compiled)

    register_block(predicate)


def clear() -> None:
    _block_predicates.clear()


def is_allowed(event: ActivityEvent) -> bool:
    return not any(pred(event) for pred in _block_predicates)
