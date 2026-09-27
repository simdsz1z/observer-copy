"""Small always-on-top window for the local Observer dashboard."""

from __future__ import annotations

import argparse
import ctypes
import json
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import webbrowser
import sys


def run(port: int) -> None:
    root = tk.Tk()
    root.title("Observer companion")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg="#0d1425")
    width, height = 312, 250
    root.geometry(f"{width}x{height}+{root.winfo_screenwidth() - width - 25}+{root.winfo_screenheight() - height - 75}")
    messages: queue.Queue[dict | None] = queue.Queue()
    stop = threading.Event()
    drag = {"x": 0, "y": 0}

    def label(parent, text="", size=10, color="#dfe8f8", weight="normal", wrap=280):
        return tk.Label(parent, text=text, bg="#0d1425", fg=color, font=("Segoe UI", size, weight), anchor="w", justify="left", wraplength=wrap)

    frame = tk.Frame(root, bg="#0d1425", highlightbackground="#354765", highlightthickness=1, padx=16, pady=12)
    frame.pack(fill="both", expand=True)
    header = tk.Frame(frame, bg="#0d1425")
    header.pack(fill="x")
    title = label(header, "◉  Observer companion", 12, "#a9c2ff", "bold")
    title.pack(side="left")
    close = tk.Button(header, text="×", command=root.destroy, bg="#0d1425", fg="#9eabc2", activebackground="#253553", activeforeground="white", borderwidth=0, font=("Segoe UI", 17))
    close.pack(side="right")
    status = label(frame, "Connecting…", 9, "#83e0b6")
    status.pack(fill="x", pady=(12, 6))
    task = label(frame, "Set a study goal in the dashboard.", 12, "#f2f5fd", "bold")
    task.pack(fill="x")
    state = label(frame, "", 9, "#9fb0ce")
    state.pack(fill="x", pady=(6, 0))
    feedback = label(frame, "", 10, "#c2d3f7")
    feedback.pack(fill="x", pady=(12, 0))
    open_button = tk.Button(frame, text="Open dashboard ↗", command=lambda: webbrowser.open(f"http://127.0.0.1:{port}/"), bg="#253858", fg="#dce7fa", activebackground="#324d78", activeforeground="white", borderwidth=0, padx=12, pady=6, font=("Segoe UI", 9, "bold"))
    open_button.pack(side="bottom", anchor="w", pady=(8, 0))

    def begin_drag(event):
        drag["x"], drag["y"] = event.x_root - root.winfo_x(), event.y_root - root.winfo_y()

    def move_drag(event):
        root.geometry(f"+{event.x_root - drag['x']}+{event.y_root - drag['y']}")

    for target in (header, title):
        target.bind("<ButtonPress-1>", begin_drag)
        target.bind("<B1-Motion>", move_drag)

    def worker():
        while not stop.is_set():
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/companion", timeout=3) as response:
                    messages.put(json.load(response))
            except (urllib.error.URLError, OSError, ValueError):
                messages.put(None)
            stop.wait(5)

    def update():
        while not messages.empty():
            data = messages.get_nowait()
            if data is None:
                status.configure(text="Dashboard offline", fg="#f0aaad")
                state.configure(text="Start the Observer dashboard to reconnect.")
                continue
            monitoring = data["status"]["monitoring"]
            study = data["status"]["study"]
            behavior = data["behavior"]
            current = data["current"] or {}
            session = behavior["session"]
            status.configure(text=("● Monitoring" if monitoring else "● Paused") + ("  ·  Study session" if session else ""), fg="#83e0b6" if monitoring else "#a2acc0")
            task.configure(text=study.get("task") or study.get("goal") or "Observation mode")
            category = behavior["latest_category"].replace("_", " ")
            state.configure(text=f"Now: {current.get('current_application') or '—'}  ·  {category}")
            model_feedback = data.get("ai_feedback")
            if model_feedback:
                tip = model_feedback["feedback"].strip().replace("\n", " ")
                source = "MiniMax" if model_feedback["source"] == "minimax" else "Local AI"
                feedback.configure(text=f"{source}: {tip[:220]}{'…' if len(tip) > 220 else ''}")
            else:
                journal = data.get("journal") or {}
                if not study.get("goal") and journal.get("observations"):
                    feedback.configure(text=journal["observations"][0])
                else:
                    feedback.configure(text=(data.get("feedback") or ["No feedback yet."])[0])
        if root.winfo_exists():
            root.after(250, update)

    def close_window():
        stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close_window)
    close.configure(command=close_window)
    threading.Thread(target=worker, daemon=True).start()
    root.after(250, update)
    root.mainloop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    port = parser.parse_args().port
    mutex = None
    if sys.platform == "win32":
        mutex = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\ObserverCopyCompanion")
        if ctypes.windll.kernel32.GetLastError() == 183:
            sys.exit(0)
    try:
        run(port)
    finally:
        if mutex:
            ctypes.windll.kernel32.ReleaseMutex(mutex)
            ctypes.windll.kernel32.CloseHandle(mutex)
