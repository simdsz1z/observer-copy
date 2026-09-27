"""Orchestrator for collectors, privacy filters, and local storage."""

from __future__ import annotations

import os as _os
import threading
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from . import privacy
from .collectors import (
    DEFAULT_COLLECTORS,
    _BaseCollector,
)
from .context import ContextEngine
from .database import connect, record_event, record_snapshot
from .models import ActivityEvent, ContextSnapshot

# Set ``OBSERVER_DEBUG_LOG`` (env var) to a path to trace every start/stop
# call with the caller's stack frame. Used to diagnose "the observer turns
# itself off" — leave the env var unset in production for zero overhead.
_DEBUG_LOG = _os.environ.get("OBSERVER_DEBUG_LOG")


def _trace(action: str) -> None:
    if not _DEBUG_LOG:
        return
    try:
        stack = "".join(traceback.format_stack(limit=6)[-3:-1])
        with open(_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.time():.3f}] {action}\n{stack}\n")
    except Exception:  # noqa: BLE001
        pass


class Observer:
    """The public-facing Observer AI object.

    Construct with ``Observer(db_path=...)``, then call ``start()`` and
    later ``stop()``. Inspect state with ``snapshot()`` or
    ``collector_status()``.
    """

    def __init__(
        self,
        db_path: str | Path | None = None,
        engine: ContextEngine | None = None,
        collectors: list | None = None,
        root: str | None = None,
    ) -> None:
        self.engine = engine or ContextEngine()
        self._db_path = Path(db_path) if db_path else Path("observer.db")
        self._db_conn = connect(self._db_path)
        self._lock = threading.RLock()
        self._collectors: list[_BaseCollector] = []
        self._collectors_factory: list[Callable[[Callable[[ActivityEvent], None]], _BaseCollector]] = (
            list(collectors) if collectors is not None else list(DEFAULT_COLLECTORS)
        )
        self._monitoring = False
        self._root = root
        self._privacy_registered = False

    # -- lifecycle ------------------------------------------------------

    def register_default_privacy(self) -> None:
        """Block well-known noisy or sensitive applications. Idempotent."""
        with self._lock:
            if self._privacy_registered:
                return
            privacy.block_application("1Password", "KeePass", "Bitwarden")
            privacy.block_window_title(r"(?i)password", r"(?i)secret", r"(?i)api[-_ ]?key")
            self._privacy_registered = True

    def start(self) -> None:
        with self._lock:
            if self._monitoring:
                return
            self.register_default_privacy()
            for factory in self._collectors_factory:
                try:
                    collector = factory(self._on_event)
                except Exception:  # noqa: BLE001
                    continue
                self._collectors.append(collector)
                collector.start()
            self._monitoring = True
            _trace("START")

    def stop(self) -> None:
        _trace(f"STOP_REQUESTED was_monitoring={self._monitoring}")
        with self._lock:
            if not self._monitoring:
                return
            for collector in self._collectors:
                collector.stop()
            for collector in self._collectors:
                collector.join(timeout=2.0)
            self._collectors.clear()
            try:
                self._db_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._monitoring = False

    # -- events ---------------------------------------------------------

    def _on_event(self, event: ActivityEvent) -> None:
        import os as _o_dbg
        _log = _o_dbg.environ.get("OBSERVER_DEBUG_LOG")
        if not privacy.is_allowed(event):
            if _log:
                try:
                    with open(_log, "a", encoding="utf-8") as _f:
                        _f.write(f"[on_event] BLOCKED by privacy: {event}\n")
                except Exception:  # noqa: BLE001
                    pass
            return
        self.engine.update(event)
        try:
            record_event(self._db_conn, event)
            record_snapshot(self._db_conn, self.engine.snapshot())
            if _log:
                try:
                    with open(_log, "a", encoding="utf-8") as _f:
                        _f.write(f"[on_event] wrote: {event.source}/{event.event_type} app={event.application}\n")
                except Exception:  # noqa: BLE001
                    pass
        except Exception as _e:  # noqa: BLE001 - log instead of swallow
            if _log:
                try:
                    with open(_log, "a", encoding="utf-8") as _f:
                        _f.write(f"[on_event] FAILED: {_e!r}\n")
                except Exception:  # noqa: BLE001
                    pass

    # -- queries --------------------------------------------------------

    def snapshot(self) -> ContextSnapshot:
        return self.engine.snapshot()

    def collector_status(self) -> list[dict]:
        """Return [{name, running}, ...] for the GUI's status row."""
        with self._lock:
            return [
                {"name": c.name, "running": c.is_running() and self._monitoring}
                for c in self._collectors
            ]

    def collector_count(self) -> int:
        return len(self._collectors_factory)

    def is_monitoring(self) -> bool:
        return self._monitoring

    # -- context manager ------------------------------------------------

    def __enter__(self) -> "Observer":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()
