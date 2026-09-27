"""Activity collectors that send local events to the context engine."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes
import os
import threading
import time
from collections.abc import Callable

from ..models import ActivityEvent, EventType


def _current_window_title() -> tuple[str | None, str | None]:
    """Return (application, window_title) for the foreground window on Windows.

    Uses Win32 APIs only; on other platforms returns (None, None).
    """
    if os.name != "nt":
        return (None, None)
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    user32.SetForegroundWindow.argtypes = [ctypes.wintypes.HWND]
    user32.SetForegroundWindow.restype = ctypes.wintypes.BOOL
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return (None, None)
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return (None, None)
    buff = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buff, length + 1)
    title = buff.value or None
    # Best-effort application name from the process PID.
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    app_name: str | None = None
    PROCESS_QUERY_LIMITED = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, pid.value)
    if handle:
        try:
            size = 512
            name = ctypes.create_unicode_buffer(size)
            if kernel32.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(ctypes.wintypes.DWORD(size))):
                app_name = os.path.basename(name.value or "") or None
        finally:
            kernel32.CloseHandle(handle)
    return (app_name, title)


class _BaseCollector(threading.Thread):
    """Boilerplate for a polling collector."""

    def __init__(self, name: str, sink: Callable[[ActivityEvent], None], interval: float = 1.5):
        super().__init__(name=name, daemon=True)
        self._sink = sink
        self._interval = interval
        self._stop_event = threading.Event()
        self.last_event: ActivityEvent | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def is_running(self) -> bool:
        return not self._stop_event.is_set()

    def _emit(self, event: ActivityEvent) -> None:
        self.last_event = event
        self._sink(event)

    def run(self) -> None:  # type: ignore[override]
        import os as _o_dbg
        _log = _o_dbg.environ.get("OBSERVER_DEBUG_LOG")
        _tick_n = 0
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as _exc:  # noqa: BLE001 - log instead of swallow
                if _log:
                    try:
                        with open(_log, "a", encoding="utf-8") as _f:
                            _f.write(f"[{self.name}] _tick failed: {_exc!r}\n")
                    except Exception:  # noqa: BLE001
                        pass
            _tick_n += 1
            if _log and _tick_n % 10 == 0:
                try:
                    with open(_log, "a", encoding="utf-8") as _f:
                        _f.write(f"[{self.name}] alive, _tick_n={_tick_n}, last_event={self.last_event}\n")
                except Exception:  # noqa: BLE001
                    pass
            self._stop_event.wait(self._interval)

    def _tick(self) -> None:
        raise NotImplementedError


class ForegroundCollector(_BaseCollector):
    """Polls the foreground window and pushes APP_FOCUS events."""

    def __init__(self, sink: Callable[[ActivityEvent], None]):
        super().__init__("ForegroundCollector", sink, interval=2.0)
        self._last_app: str | None = None
        self._last_title: str | None = None

    def _tick(self) -> None:
        app, title = _current_window_title()
        if app == self._last_app and title == self._last_title:
            return
        self._last_app = app
        self._last_title = title
        self._emit(
            ActivityEvent(
                timestamp=time.time(),
                source="foreground",
                event_type=EventType.APP_FOCUS,
                application=app,
                window_title=title,
            )
        )


class GitCollector(_BaseCollector):
    """Watches the active git repo for branch / uncommitted-changes events."""

    def __init__(self, sink: Callable[[ActivityEvent], None], cwd: str | None = None):
        super().__init__("GitCollector", sink, interval=5.0)
        self._cwd = cwd or os.getcwd()
        self._last_signature: tuple | None = None

    def _tick(self) -> None:
        import subprocess

        try:
            branch_proc = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=3,
            )
            branch = branch_proc.stdout.strip() if branch_proc.returncode == 0 else None
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return

        if not branch:
            return

        try:
            status_proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=3,
            )
            changed = [
                line.strip().split(" ", 1)[-1]
                for line in (status_proc.stdout or "").splitlines()
                if line.strip()
            ]
        except subprocess.TimeoutExpired:
            changed = []

        try:
            log_proc = subprocess.run(
                ["git", "log", "-1", "--pretty=%h %s"],
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=3,
            )
            latest_commit = log_proc.stdout.strip() if log_proc.returncode == 0 else None
        except subprocess.TimeoutExpired:
            latest_commit = None

        signature = (branch, tuple(changed), latest_commit)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        project_name = os.path.basename(self._cwd.rstrip(os.sep)) or None
        self._emit(
            ActivityEvent(
                timestamp=time.time(),
                source="git",
                event_type=EventType.GIT_STATE,
                project=project_name,
                project_path=self._cwd,
                metadata={
                    "branch": branch,
                    "has_uncommitted": bool(changed),
                    "changed_files": changed,
                    "latest_commit": latest_commit,
                },
            )
        )


class FilesystemCollector(_BaseCollector):
    """Polls a project root for FILE_EDIT-ish events. Cheap heuristic: track
    mtime of every file under root; emit when a watched file's mtime jumps.
    """

    def __init__(self, sink: Callable[[ActivityEvent], None], root: str | None = None):
        super().__init__("FilesystemCollector", sink, interval=4.0)
        self._root = root or os.getcwd()
        self._seen: dict[str, float] = {}

    def _tick(self) -> None:
        for dirpath, _, filenames in os.walk(self._root):
            # Don't dive into .git, node_modules, .venv — too noisy.
            if any(skip in dirpath for skip in (os.sep + ".git", os.sep + "node_modules", os.sep + ".venv", os.sep + "__pycache__")):
                continue
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    mtime = os.path.getmtime(full)
                except OSError:
                    continue
                prev = self._seen.get(full)
                self._seen[full] = mtime
                if prev is None or mtime - prev < 0.01:
                    continue
                self._emit(
                    ActivityEvent(
                        timestamp=mtime,
                        source="filesystem",
                        event_type=EventType.FILE_EDIT,
                        project=os.path.basename(self._root.rstrip(os.sep)),
                        project_path=self._root,
                        metadata={"path": os.path.relpath(full, self._root)},
                    )
                )


class HeartbeatCollector(_BaseCollector):
    """Emits a low-volume heartbeat so the engine knows it is alive even when
    the foreground and git collectors are quiet.
    """

    def __init__(self, sink: Callable[[ActivityEvent], None]):
        super().__init__("HeartbeatCollector", sink, interval=10.0)
        self._tick_count = 0

    def _tick(self) -> None:
        self._tick_count += 1
        self._emit(
            ActivityEvent(
                timestamp=time.time(),
                source="heartbeat",
                event_type=EventType.COMMAND_RUN,
                metadata={"tick": self._tick_count},
            )
        )


DEFAULT_COLLECTORS = [ForegroundCollector, GitCollector, FilesystemCollector, HeartbeatCollector]
