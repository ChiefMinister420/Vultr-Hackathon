let current = null;
let poll = null;

const $ = (id) => document.getElementById(id);

async function api(path, data) {
  const response = await fetch(path, {
    method: data ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json' },
    body: data ? JSON.stringify(data) : undefined
  });
  const result = await response.json();
  if (!response.ok) throw Error(result.error || 'Request failed');
  return result;
}

function showBanner(message) {
  $('banner').textContent = message;
  $('banner').classList.remove('hidden');
}

async function updateHealth() {
  try {
    const health = await api('/api/health');

    $('engine').textContent = health.docker_ready
      ? 'DOCKER RUNNER READY'
      : 'DOCKER RUNNER OFFLINE';

    if (!health.docker_ready) {
      showBanner('Docker is not connected. You can review the permission slip, but execution needs the Docker runner. No task will run without it.');
    }

    updateInferenceIndicator(health.inference);
  } catch (error) {
    $('engine').textContent = 'BACKEND OFFLINE';
    $('inference-label').textContent = 'VULTR STATUS UNAVAILABLE';
    $('inference-dot').className = 'dot error';
    console.error('Health check failed:', error);
  }
}

function updateInferenceIndicator(inference) {
  const dot = $('inference-dot');
  const label = $('inference-label');

  if (!inference || !inference.configured) {
    dot.className = 'dot';
    label.textContent = 'VULTR NOT CONFIGURED · LOCAL PLANNER';
    return;
  }

  if (inference.connected) {
    dot.className = 'dot live';
    label.textContent = `VULTR CONNECTED · ${inference.model}`;
    return;
  }

  if (inference.status === 'connecting') {
    dot.className = 'dot';
    label.textContent = 'CONNECTING TO VULTR…';
    return;
  }

  if (inference.status === 'error') {
    dot.className = 'dot error';
    label.textContent = 'VULTR UNAVAILABLE · LOCAL FALLBACK';
    return;
  }

  dot.className = 'dot';
  label.textContent = `VULTR CONFIGURED · NOT TESTED · ${inference.model}`;
}

updateHealth();
setInterval(updateHealth, 5000);

$('plan').onclick = async () => {
  try {
    $('plan').disabled = true;
    $('banner').classList.add('hidden');

    current = await api('/api/plan', { task: $('task').value });

    $('planner').textContent = 'Planned by ' + current.planner;
    $('time').value = current.manifest.time_seconds;
    $('time').max = current.manifest.time_seconds;
    $('manifest').classList.remove('hidden');
    $('output').classList.add('hidden');
    renderEvents(current.events);
    $('state').textContent = 'REVIEW';
    $('dot').className = 'dot';

    // The health endpoint reflects whether the most recent inference request worked.
    await updateHealth();
  } catch (error) {
    showBanner(error.message);
  } finally {
    $('plan').disabled = false;
  }
};

$('reject').onclick = () => {
  if (!current) return;
  $('manifest').classList.add('hidden');
  $('state').textContent = 'REJECTED';
  current = null;
};

$('approve').onclick = async () => {
  if (!current) return;

  try {
    await api('/api/approve', {
      id: current.id,
      time_seconds: Number($('time').value)
    });

    $('manifest').classList.add('hidden');
    $('state').textContent = 'RUNNING';
    $('dot').className = 'dot live';
    $('events').innerHTML = '';

    clearInterval(poll);
    poll = setInterval(() => refresh(current.id), 650);
    await refresh(current.id);
  } catch (error) {
    showBanner(error.message);
  }
};

async function refresh(id) {
  try {
    const run = await api('/api/runs/' + id);
    renderEvents(run.events);

    if (['completed', 'failed', 'timeout'].includes(run.state)) {
      clearInterval(poll);
      $('state').textContent = run.state.toUpperCase();
      $('dot').className = 'dot';

      if (run.result?.artifact) renderChart(run.result.artifact);

      if (run.report) {
        $('output').classList.remove('hidden');
        $('audit').textContent = JSON.stringify({
          allowed: run.report.allowed,
          used: run.report.used,
          blocked_attempts: run.report.blocked_attempts,
          cleanup: run.report.cleanup,
          error: run.error || null
        }, null, 2);
      }

      if (run.error) showBanner(run.error);
    }
  } catch (error) {
    clearInterval(poll);
    showBanner(error.message);
  }
}

function renderEvents(events) {
  $('events').innerHTML = events.map((event) => `
    <div class="event">
      <i>›</i>
      <span>${new Date(event.at * 1000).toLocaleTimeString()} &nbsp; ${escapeHtml(event.text)}</span>
    </div>
  `).join('');
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (character) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);
}

function renderChart(data) {
  const max = Math.max(...data.values, 1);

  $('chart').innerHTML = data.values.map((value, index) => `
    <div class="barwrap">
      <div class="bar" style="height:${Math.max(7, value / max * 125)}px" title="$${value.toFixed(2)}"></div>
      ${escapeHtml(data.labels[index])}
    </div>
  `).join('');

  $('table').textContent =
    `${data.rows_processed} rows · ${data.labels.length} ranked products · ${data.currency}`;
}