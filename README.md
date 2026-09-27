# Observer AI dashboard

Observer AI is a local activity dashboard. It stores its observations in
`data/observer.db`.

## License

The source is available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).
You may view, use, modify, and redistribute it for permitted noncommercial
purposes. Commercial use requires separate permission from the project owner.
This is a source-available license, not an OSI-approved open-source license.

## Start

On Windows, double-click **Start Observer Dashboard.bat**. The dashboard opens
at <http://127.0.0.1:8765/>. Keep the terminal window open while using it.
You can also run `python dashboard.py` from this folder. Python 3.10 or newer
is recommended. No packages need to be installed.

Click **Start monitoring** to collect application focus events. Enter an
existing project folder and click **Save folder** to include file changes and
Git state. Stop monitoring before changing the folder. The dashboard refreshes
every five seconds. Click **Stop monitoring** or close the terminal to stop.

## Study companion

Enter a goal, current task, apps you use to study, and apps you want to limit.
Click **Save plan**, then **Start study session**. Observer samples the active
app every 30 seconds and marks the sample as focus, distraction, away, or
unclassified. The session totals and seven-day heatmap use those samples.
They estimate app use, not attention or intent. App lists are yours to define.

Observer does not ask for recurring check-ins. During a session it automatically
shows sampled app categories, recent app changes, and counts of file edits and
Git state events from the watched project. MiniMax also gets a compact timeline
of recent sampled apps and cleaned browser page titles. It can distinguish a
task-related tutorial from an unrelated video using the title and the goal,
rather than treating all browser time alike. Generic or missing titles still
have insufficient context; Observer does not invent an activity. After ending
a session, the observed session summary remains visible. These signals cannot
verify attention, completion, or work quality. Older check-in records, if
present, stay in the local database; Observer no longer displays or sends them.

Click **Mini companion** for a small movable window that stays above other
windows. It shows the task, current app category, and a short feedback tip.
Close it with the X; the dashboard and monitoring continue independently.

## Activity journal without a goal

Monitoring also builds a seven-day activity journal when no goal or study
session is set. It shows sampled active and away time, app changes, the most
sampled app each day, and browser page titles that appeared in at least two focus
events. The mini companion shows the most concrete current observation in this
mode. Repeated titles are clues about what was opened repeatedly; they do not
prove interest, intent, or productivity. The journal is computed locally from
the existing database. During a study session, cleaned browser page titles
may also be included in MiniMax feedback requests as described below.

The feedback panel always offers rule-based tips. In **AI settings**, choose
**MiniMax cloud**, select a model, paste your MiniMax API key, and click **Save
AI settings**. Then **Ask MiniMax** sends your goal, task, aggregate session
categories, recent app switching, watched-project event counts, and a short
list of sampled app types and cleaned browser page titles to MiniMax. It strips
email addresses, URLs, file paths, and sensitive titles before sending them.
Other application titles and raw file paths stay local. Do not put confidential
information in the goal or task if you use MiniMax cloud.
The key is encrypted with Windows DPAPI for the current user in
`data/minimax-key.bin`; it is never returned to the browser. Use **Remove saved
key** to delete it. Automatic feedback is enabled by default. While monitoring
and a study session are active, Observer waits for about five minutes of recent
activity, then asks the selected model for feedback in the background. It needs
another five minutes of new activity and waits at least 15 minutes before the
next request. It stops at 12 requests per day, waits 30 minutes after a failed
request, and skips checks while you are away. The latest response appears in
the dashboard and mini companion. You can switch automatic feedback off in
AI settings or press **Ask MiniMax** to request feedback yourself. Feedback
history and scheduling data stay in `data/ai-feedback.json`.

You can instead choose **LM Studio on this computer**. That option uses a local
model if LM Studio's server is running at `127.0.0.1:1234` with a model loaded.
If the selected model service is unavailable, the panel keeps the rule-based
tips and explains the problem.

The dashboard listens only on the local computer. It records app names, window
titles, and optional project file paths in its local SQLite database. Its
built-in privacy filter skips common password managers and titles containing
password, secret, or API key. Clear `data/observer.db` while the program is
stopped if you want to remove its history.
