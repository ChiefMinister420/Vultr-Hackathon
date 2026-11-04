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
  const name = inference?.provider === 'groq' ? 'GROQ (DEV ONLY)' : 'VULTR';

  if (inference?.error) {
    dot.className = 'dot error';
    label.textContent = 'LLM CONFIG REFUSED · LOCAL PLANNER';
    return;
  }

  if (!inference || !inference.configured) {
    dot.className = 'dot';
    label.textContent = `${name} NOT CONFIGURED · LOCAL PLANNER`;
    return;
  }

  if (inference.connected) {
    dot.className = 'dot live';
    label.textContent = `${name} CONNECTED · ${inference.model}`;
    return;
  }

  if (inference.status === 'connecting') {
    dot.className = 'dot';
    label.textContent = `CONNECTING TO ${name}…`;
    return;
  }

  if (inference.status === 'error') {
    dot.className = 'dot error';
    label.textContent = `${name} UNAVAILABLE · LOCAL FALLBACK`;
    return;
  }

  dot.className = 'dot';
  label.textContent = `${name} CONFIGURED · NOT TESTED · ${inference.model}`;
}

function providerLabel(provider, model) {
  if (provider === 'redteam') return 'Red team canned script (app/redteam.py)';
  if (provider === 'vultr') return `Vultr Serverless Inference (${model})`;
  if (provider === 'groq') return `DEV ONLY: Groq (${model}), not the submission provider`;
  return 'Built-in analyze.py';
}

function plannerLabel(plan) {
  if (plan.provider === 'vultr') return `Planner: ${providerLabel(plan.provider, plan.model)}`;
  if (plan.provider === 'groq') return providerLabel(plan.provider, plan.model);
  return 'Planned by ' + plan.planner;
}

function renderMode(plan) {
  $('redteam-tag').classList.toggle('hidden', plan.mode !== 'redteam');
  if (plan.mode === 'redteam') {
    $('mode').textContent = `Code: RED TEAM SELF-TEST "${plan.redteam_scenario}", canned script from app/redteam.py (no model call)`;
    $('script-summary').textContent = 'Red team script (canned and harmless; runs in the sandbox under this manifest)';
    $('script').textContent = plan.script;
    $('script-box').classList.remove('hidden');
  } else if (plan.mode === 'model-written') {
    $('mode').textContent = 'Code: model-written by ' + providerLabel(plan.codegen.provider, plan.codegen.model);
    $('script-summary').textContent = 'Model-written script (runs in the sandbox under this manifest; approval does not change it)';
    $('script').textContent = plan.script;
    $('script-box').classList.remove('hidden');
  } else {
    $('mode').textContent = 'Code: built-in analyze.py (no model-written code)'
      + (plan.codegen_error ? ' · generation failed: ' + plan.codegen_error : '');
    $('script-box').classList.add('hidden');
  }
}

function renderAttempts(run) {
  $('attempts').textContent = (run.attempts || []).map((a) =>
    `#${a.attempt} ${a.status} · exit ${a.exit_code ?? '-'}${a.killed ? ' (killed)' : ''} · `
    + `${a.elapsed_seconds}s · ${providerLabel(a.provider, a.model)}`
  ).join('\n') || 'No attempt ran.';
  $('final').textContent = run.final_answer || '(no output)';
}

updateHealth();
setInterval(updateHealth, 5000);

function renderManifest(manifest) {
  const hosts = manifest.network_domains || [];
  $('net-value').innerHTML = hosts.length
    ? `${escapeHtml(hosts.join(', '))} <small>· allowlist proxy only</small>`
    : 'None <small>· isolated</small>';
  $('mem-value').innerHTML = `${escapeHtml(manifest.memory_mb)} <small>MB / max 512</small>`;
  $('cpu-value').innerHTML = `${escapeHtml(manifest.cpu_cores)} <small>core / max 1</small>`;
}

function showPlan(plan) {
  current = plan;
  $('planner').textContent = plannerLabel(plan);
  renderMode(plan);
  renderManifest(plan.manifest);
  $('time').value = plan.manifest.time_seconds;
  $('time').max = plan.manifest.time_seconds;
  $('manifest').classList.remove('hidden');
  $('output').classList.add('hidden');
  renderEvents(plan.events);
  $('state').textContent = plan.mode === 'redteam' ? 'RED TEAM · REVIEW' : 'REVIEW';
  $('dot').className = 'dot';
}

$('plan').onclick = async () => {
  try {
    $('plan').disabled = true;
    $('banner').classList.add('hidden');

    showPlan(await api('/api/plan', { task: $('task').value }));

    // The health endpoint reflects whether the most recent inference request worked.
    await updateHealth();
  } catch (error) {
    showBanner(error.message);
  } finally {
    $('plan').disabled = false;
  }
};

document.querySelectorAll('[data-scenario]').forEach((button) => {
  button.onclick = async () => {
    try {
      button.disabled = true;
      $('banner').classList.add('hidden');
      showPlan(await api('/api/redteam', { scenario: button.dataset.scenario }));
    } catch (error) {
      showBanner(error.message);
    } finally {
      button.disabled = false;
    }
  };
});

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
      $('state').textContent = (run.mode === 'redteam' ? 'RED TEAM · ' : '') + run.state.toUpperCase();
      $('dot').className = 'dot';

      $('chart').innerHTML = '';
      $('table').textContent = '';
      if (run.result?.artifact) renderChart(run.result.artifact);
      renderAttempts(run);
      renderNetwork(run.report?.network);
      renderRedteam(run);

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

function renderNetwork(network) {
  $('network-report').classList.toggle('hidden', !network);
  if (!network) return;
  const list = (hosts) => hosts.length ? escapeHtml(hosts.join(', ')) : 'none';
  const blocked = network.blocked.map((b) => `
    <tr class="blocked">
      <td>${escapeHtml(b.host ?? '?')}${b.port ? ':' + escapeHtml(b.port) : ''}</td>
      <td>${escapeHtml(b.reason ?? '')}</td>
    </tr>
  `).join('');
  $('network').innerHTML = `
    <div class="table">
      Mode: ${escapeHtml(network.mode)}<br>
      Allowed hosts: ${list(network.allowed)}<br>
      Hosts actually used: ${list(network.used)}<br>
      Blocked attempts: ${network.blocked.length}
    </div>
    ${blocked ? `<table class="scorecard"><tr><th>Blocked host</th><th>Proxy reason</th></tr>${blocked}</table>` : ''}
  `;
}

function renderRedteam(run) {
  const isRedteam = run.mode === 'redteam';
  $('redteam-report').classList.toggle('hidden', !isRedteam);
  if (!isRedteam) return;

  const outcome = run.redteam_outcome;
  $('redteam-outcome').textContent = outcome || '';
  $('redteam-outcome').classList.toggle('hidden', !outcome);

  const attempts = run.redteam_attempts || [];
  $('scorecard').classList.toggle('hidden', !attempts.length);
  $('scorecard').innerHTML = attempts.length ? `
    <tr><th>Attempt</th><th>Result</th><th>Expected</th><th>Detail</th></tr>
    ${attempts.map((a) => `
      <tr>
        <td>${escapeHtml(a.name)}</td>
        <td><span class="badge ${a.result === 'OK' ? 'ok' : 'blocked'}">${escapeHtml(a.result)}</span></td>
        <td>${a.as_expected ? '✓ ' + escapeHtml(a.expected) : '<span class="danger">✗ expected ' + escapeHtml(a.expected) + '</span>'}</td>
        <td>${escapeHtml(a.detail)}</td>
      </tr>
    `).join('')}
  ` : '';
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
    `${data.rows_processed ?? '?'} rows · ${data.labels.length} ranked products · ${data.currency ?? ''}`;
}