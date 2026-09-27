const $ = (id) => document.getElementById(id);
let busy = false;
let editingFolder = false;
let editingStudy = false;
let editingAI = false;
let aiResult = null;

function showError(message) {
  $('notice').textContent = message || '';
  $('notice').classList.toggle('show', Boolean(message));
}

function display(value, fallback = '—') {
  return value === null || value === undefined || value === '' ? fallback : String(value);
}

function when(timestamp) {
  if (!timestamp) return '—';
  return new Date(timestamp * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function rankList(id, rows, key) {
  const el = $(id);
  el.replaceChildren();
  if (!rows.length) { el.className = 'list empty'; el.textContent = 'Nothing recorded yet.'; return; }
  el.className = 'list';
  const max = Math.max(...rows.map((r) => r.n), 1);
  for (const row of rows) {
    const item = document.createElement('div'); item.className = 'rank';
    const left = document.createElement('div');
    const name = document.createElement('div'); name.className = 'name'; name.textContent = display(row[key]); name.title = name.textContent;
    const meter = document.createElement('div'); meter.className = 'meter';
    const fill = document.createElement('span'); fill.style.width = `${(row.n / max) * 100}%`;
    meter.append(fill); left.append(name, meter);
    const count = document.createElement('span'); count.className = 'count'; count.textContent = row.n;
    item.append(left, count); el.append(item);
  }
}

function renderHeatmap(behavior) {
  const el = $('heatmap'); el.replaceChildren();
  const order = ['focus', 'distraction', 'unknown', 'away'];
  for (let day = 0; day < 7; day++) {
    const row = document.createElement('div'); row.className = 'hm-row';
    const label = document.createElement('span'); label.className = 'hm-label'; label.textContent = behavior.day_labels[day]; row.append(label);
    for (let hour = 0; hour < 24; hour++) {
      const cell = document.createElement('div');
      const counts = behavior.heatmap[day][hour];
      const total = order.reduce((n, category) => n + counts[category], 0);
      const category = [...order].sort((a, b) => counts[b] - counts[a])[0];
      cell.className = `hm-cell${total ? ` ${category}` : ''}`;
      cell.title = `${behavior.day_labels[day]} ${String(hour).padStart(2, '0')}:00 · ${total} samples · focus ${counts.focus}, distraction ${counts.distraction}, away ${counts.away}, other ${counts.unknown}`;
      row.append(cell);
    }
    el.append(row);
  }
}

function renderFeedback(feedback) {
  const el = $('feedback'); el.replaceChildren();
  for (const message of feedback) {
    const item = document.createElement('div'); item.className = 'feedback-item'; item.textContent = message; el.append(item);
  }
}

function renderJournal(journal) {
  const facts = $('journal-facts'); facts.replaceChildren();
  for (const message of journal.observations) {
    const row = document.createElement('div'); row.textContent = message; facts.append(row);
  }
  const days = $('journal-days'); days.replaceChildren();
  if (!journal.days.length) { days.className = 'empty'; days.textContent = 'No samples yet. Start monitoring to build the journal.'; }
  else {
    days.className = '';
    for (const day of journal.days) {
      const row = document.createElement('div'); row.className = 'journal-row';
      const title = document.createElement('strong'); title.textContent = new Date(`${day.date}T12:00:00`).toLocaleDateString([], { weekday: 'long', month: 'short', day: 'numeric' });
      const details = document.createElement('span'); details.textContent = `${day.active_minutes} min active · ${day.away_minutes} min away · ${day.switches} sampled app changes${day.top_app ? ` · most sampled: ${day.top_app} (${day.top_app_minutes} min)` : ''}`;
      row.append(title, details); days.append(row);
    }
  }
  const subjects = $('journal-subjects'); subjects.replaceChildren();
  if (!journal.subjects.length) { subjects.className = 'empty'; subjects.textContent = 'No browser page title has appeared twice yet.'; }
  else {
    subjects.className = '';
    for (const item of journal.subjects) {
      const row = document.createElement('div'); row.className = 'journal-row';
      const title = document.createElement('strong'); title.textContent = item.subject;
      const details = document.createElement('span'); details.textContent = `${item.visits} focus events · ${item.days} day${item.days === 1 ? '' : 's'}`;
      row.append(title, details); subjects.append(row);
    }
  }
}

function renderEvidence(behavior, lastReport, modelFeedback) {
  const session = behavior.session;
  const report = $('evidence-report'); report.replaceChildren();
  const lines = [];
  if (session) {
    lines.push(`${behavior.minutes.focus} min in selected focus apps · ${behavior.minutes.distraction} min in selected distraction apps · ${behavior.switches_recent} recent app changes.`);
    lines.push(`${behavior.project_events.file_edit} watched-project file edits · ${behavior.project_events.git_state} Git state events.`);
    if (modelFeedback?.analysis) {
      const labels = { work_related: 'Related to goal', off_task: 'Appears off task', insufficient_context: 'Context unavailable' };
      const minutes = { work_related: 0, off_task: 0, insufficient_context: 0 };
      for (const decision of modelFeedback.analysis.classifications) {
        const context = modelFeedback.contexts?.find((item) => item.id === decision.id);
        if (context) minutes[decision.label] += context.minutes;
      }
      lines.push(`AI assessment of recent sampled context: ${minutes.work_related.toFixed(1)} min related · ${minutes.off_task.toFixed(1)} min appears off task · ${minutes.insufficient_context.toFixed(1)} min without enough detail. These are estimates from the last analysis, not a whole-session score.`);
      for (const decision of modelFeedback.analysis.classifications.slice(0, 8)) {
        const context = modelFeedback.contexts?.find((item) => item.id === decision.id);
        if (context) lines.push(`${labels[decision.label]} · ${context.app}${context.page_title ? ` · ${context.page_title}` : ''} · ${context.minutes} min. ${decision.reason}`);
      }
    }
  } else if (lastReport) {
    lines.push(`Last session (${when(lastReport.session.ended)}): ${lastReport.sampled_minutes} min sampled · ${lastReport.minutes.focus} min in selected focus apps · ${lastReport.minutes.distraction} min in selected distraction apps · ${lastReport.project_file_edits} watched-project file edits.`);
  } else {
    lines.push('Start a study session to see automatic evidence here.');
  }
  for (const line of lines) {
    const item = document.createElement('div'); item.textContent = line; report.append(item);
  }
}

function render(data) {
  const status = data.status;
  $('live').classList.toggle('on', status.monitoring);
  $('status').textContent = status.monitoring ? 'Monitoring live' : 'Stopped';
  $('toggle').textContent = status.monitoring ? 'Stop monitoring' : 'Start monitoring';
  $('toggle').classList.toggle('stop', status.monitoring);
  $('save-folder').disabled = status.monitoring;
  $('folder').disabled = status.monitoring;
  if (!editingFolder) $('folder').value = status.watched_folder || '';
  $('db-path').textContent = `Data stored locally: ${status.database}`;
  if (!editingStudy) {
    $('goal').value = status.study.goal || '';
    $('task').value = status.study.task || '';
    $('focus-apps').value = (status.study.focus_apps || []).join(', ');
    $('distraction-apps').value = (status.study.distraction_apps || []).join(', ');
  }
  if (!editingAI) {
    $('ai-provider').value = status.study.ai_provider || 'minimax';
    $('minimax-model').value = status.study.minimax_model || 'MiniMax-M3';
    $('auto-feedback').checked = status.study.auto_feedback !== false;
  }
  $('ai-feedback').textContent = status.study.ai_provider === 'local' ? 'Ask local AI' : 'Ask MiniMax';
  $('key-status').textContent = status.has_minimax_key ? 'MiniMax key saved' : 'No MiniMax key saved';
  $('key-status').classList.toggle('missing', !status.has_minimax_key);
  const auto = data.auto_feedback || {};
  $('auto-feedback-status').textContent = `${auto.message || ''}${auto.next_at ? ` Next check ${when(auto.next_at)}.` : ''}${auto.last_error ? ` Last request: ${auto.last_error}` : ''}`;
  $('clear-key').disabled = !status.has_minimax_key;
  $('collectors').replaceChildren();
  if (status.collectors.length) {
    for (const collector of status.collectors) {
      const chip = document.createElement('span'); chip.className = `chip${collector.running ? '' : ' off'}`;
      chip.textContent = `${collector.name.replace('Collector', '')} ${collector.running ? 'active' : 'stopped'}`;
      $('collectors').append(chip);
    }
  }
  const current = data.current || {};
  $('app-label').textContent = status.monitoring ? 'Current app' : 'Last seen app';
  $('current-app').textContent = display(current.current_application);
  $('current-app').title = $('current-app').textContent;
  $('current-activity').textContent = display(current.likely_activity, 'No activity classified');
  $('event-count').textContent = data.stats.activity_events_24h;
  $('app-count').textContent = data.stats.applications_24h;
  $('project-count').textContent = data.stats.projects_24h;
  $('context-title').textContent = display(current.likely_activity || current.current_application, 'No activity yet');
  $('context-description').textContent = display(current.recommended_next_step, status.monitoring ? 'Observer is collecting activity.' : 'Start monitoring to collect local activity.');
  $('context-project').textContent = display(current.current_project);
  $('context-branch').textContent = display(current.current_branch);
  $('context-time').textContent = current.timestamp ? `Updated ${when(current.timestamp)}` : '';
  $('updated').textContent = `Dashboard refreshed ${new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`;
  const chart = $('chart'); chart.replaceChildren();
  const max = Math.max(...data.hours.map((h) => h.count), 1);
  for (const hour of data.hours) {
    const bar = document.createElement('div'); bar.className = 'bar';
    bar.style.height = `${Math.max(3, hour.count / max * 100)}%`;
    bar.title = `${when(hour.timestamp)}: ${hour.count} activity events`;
    chart.append(bar);
  }
  rankList('apps', data.apps, 'application');
  rankList('projects', data.projects, 'project');
  renderJournal(data.journal);
  const behavior = data.behavior;
  const session = behavior.session;
  $('session-toggle').textContent = session ? 'End study session' : 'Start study session';
  $('session-toggle').classList.toggle('stop', Boolean(session));
  $('session-status').textContent = session ? `Session started ${when(session.started)} · ${session.goal}` : 'No study session active';
  $('focus-minutes').textContent = `${behavior.minutes.focus} min`;
  $('distraction-minutes').textContent = `${behavior.minutes.distraction} min`;
  $('unknown-minutes').textContent = `${(behavior.minutes.unknown + behavior.minutes.possible_focus).toFixed(1)} min`;
  $('away-minutes').textContent = `${behavior.minutes.away} min`;
  renderEvidence(behavior, data.last_session_report, data.ai_feedback);
  renderHeatmap(behavior);
  const modelFeedback = data.ai_feedback;
  if (modelFeedback) aiResult = null;
  renderFeedback(modelFeedback ? [modelFeedback.feedback] : aiResult ? [aiResult.feedback] : data.feedback);
  $('feedback-meta').textContent = modelFeedback ? `${modelFeedback.automatic ? 'Automatic' : 'Requested'} ${modelFeedback.source === 'minimax' ? 'MiniMax' : 'local AI'} feedback · ${when(modelFeedback.timestamp)}${modelFeedback.analysis ? ` · ${modelFeedback.analysis.confidence} confidence` : ''}. Observed activity cannot prove attention.` : aiResult?.note || 'AI feedback uses observed activity and page context. It cannot verify attention or work quality.';
  const events = $('events'); events.replaceChildren();
  if (!data.events.length) { events.className = 'events empty'; events.textContent = 'No activity recorded yet. Start monitoring and use your computer.'; }
  else {
    events.className = 'events';
    for (const event of data.events) {
      const row = document.createElement('div'); row.className = 'event';
      const time = document.createElement('time'); time.textContent = new Date(event.timestamp * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = event.event_type.replaceAll('_', ' ');
      const main = document.createElement('span'); main.className = 'event-main'; main.textContent = display(event.application || event.project || event.metadata.path, event.source); main.title = main.textContent;
      const side = document.createElement('span'); side.className = 'event-side'; side.textContent = display(event.window_title || event.project_path, ''); side.title = side.textContent;
      row.append(time, tag, main, side); events.append(row);
    }
  }
}

async function refresh() {
  if (busy) return;
  try {
    const response = await fetch('/api/overview', { cache: 'no-store' });
    if (!response.ok) throw new Error(`Dashboard request failed (${response.status}).`);
    render(await response.json());
    showError('');
  } catch (error) { showError(error.message); }
}

async function action(path, body = {}) {
  if (busy) return;
  busy = true; $('toggle').disabled = true;
  try {
    const response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Action failed.');
    showError('');
    if (path === '/api/folder') editingFolder = false;
    if (path === '/api/study') editingStudy = false;
    if (path === '/api/ai/settings') editingAI = false;
    if (path === '/api/study' || path.startsWith('/api/session/') || path.startsWith('/api/ai/')) aiResult = null;
    if (path === '/api/ai/settings') $('minimax-key').value = '';
    await refreshAfterAction();
  } catch (error) { showError(error.message); }
  finally { busy = false; $('toggle').disabled = false; }
}

async function refreshAfterAction() {
  const response = await fetch('/api/overview', { cache: 'no-store' });
  if (!response.ok) throw new Error('Could not refresh the dashboard.');
  render(await response.json());
}

$('toggle').addEventListener('click', () => action($('live').classList.contains('on') ? '/api/stop' : '/api/start'));
$('save-folder').addEventListener('click', () => action('/api/folder', { folder: $('folder').value }));
$('folder').addEventListener('input', () => { editingFolder = true; });
for (const id of ['ai-provider', 'minimax-model', 'auto-feedback']) $(id).addEventListener('change', () => { editingAI = true; });
$('save-ai').addEventListener('click', () => action('/api/ai/settings', { provider: $('ai-provider').value, model: $('minimax-model').value, api_key: $('minimax-key').value, auto_feedback: $('auto-feedback').checked }));
$('clear-key').addEventListener('click', () => action('/api/ai/key/clear'));
for (const id of ['goal', 'task', 'focus-apps', 'distraction-apps']) $(id).addEventListener('input', () => { editingStudy = true; });
$('save-study').addEventListener('click', () => action('/api/study', {
  goal: $('goal').value, task: $('task').value,
  focus_apps: $('focus-apps').value.split(',').map((x) => x.trim()).filter(Boolean),
  distraction_apps: $('distraction-apps').value.split(',').map((x) => x.trim()).filter(Boolean),
}));
$('session-toggle').addEventListener('click', () => {
  if (!$('session-toggle').textContent.startsWith('End')) return action('/api/session/start');
  action('/api/session/end');
});
$('open-companion').addEventListener('click', () => action('/api/companion/open'));
$('ai-feedback').addEventListener('click', async () => {
  if (busy) return;
  $('ai-feedback').disabled = true;
  $('ai-feedback').textContent = 'Thinking…';
  try {
    const response = await fetch('/api/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || 'Feedback request failed.');
    aiResult = { ...result, feedback: Array.isArray(result.feedback) ? result.feedback.join(' ') : result.feedback };
    await refreshAfterAction();
  } catch (error) { showError(error.message); }
  finally { $('ai-feedback').disabled = false; $('ai-feedback').textContent = $('ai-provider').value === 'local' ? 'Ask local AI' : 'Ask MiniMax'; }
});
refresh();
setInterval(refresh, 5000);
