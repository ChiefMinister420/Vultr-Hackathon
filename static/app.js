let current = null;
let poll = null;

const $ = (id) => document.getElementById(id);
const reducedMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
const scrollBehavior = () => (reducedMotion() ? 'auto' : 'smooth');

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
  $('banner').textContent = plainError(message);
  $('banner').classList.remove('hidden');
}

// Server messages that name internal fields, in plain words. Anything else is shown as sent.
const FIELD_WORDS = {
  memory_mb: ['memory', 'MB'],
  cpu_cores: ['CPU', 'cores'],
  time_seconds: ['time limit', 'seconds'],
};

function plainError(message) {
  const text = String(message ?? '');
  let match = text.match(/^(memory_mb|cpu_cores|time_seconds) (\S+) exceeds the issued (\S+);/);
  if (match) {
    const [what, unit] = FIELD_WORDS[match[1]];
    return `Approval can only lower the ${what}: ${match[2]} ${unit} is more than the ${match[3]} ${unit} on the permission slip.`;
  }
  match = text.match(/^(memory_mb|cpu_cores|time_seconds) must be at least (\S+)\.$/);
  if (match) {
    const [what, unit] = FIELD_WORDS[match[1]];
    return `The ${what} must be at least ${match[2]} ${unit}.`;
  }
  match = text.match(/^(memory_mb|cpu_cores|time_seconds) must be a number\.$/);
  if (match) return `The ${FIELD_WORDS[match[1]][0]} must be a number.`;
  if (/^network_domains .* were not in the issued manifest/.test(text)) {
    return 'Approval can only remove websites from the permission slip, not add new ones.';
  }
  if (text === 'network_domains must be a list of hostnames.') return 'The website list could not be read.';
  if (text === 'Manifest is no longer awaiting approval.') return 'This permission slip was already approved or rejected.';
  if (text === 'Unknown manifest.') return 'This permission slip has expired. Draft a new one.';
  if (text === 'invalid JSON') return 'The request could not be read. Reload the page and try again.';
  return text;
}

const LEVEL_LABELS = { ok: 'OK', note: 'NOTE', blocked: 'BLOCKED', problem: 'PROBLEM', result: 'RESULT' };

function renderReport(run) {
  const points = Array.isArray(run.report_points) ? run.report_points : null;
  $('report-fallback').classList.toggle('hidden', Boolean(points));
  $('report-points').classList.toggle('hidden', !points);
  $('report-points').innerHTML = (points || []).map((point) => {
    const level = LEVEL_LABELS[point.level] ? point.level : 'note';
    return `
      <li class="point point-${level}">
        <span class="level level-${level}">${LEVEL_LABELS[level]}</span>
        <span class="point-text">${escapeHtml(point.text)}</span>
      </li>`;
  }).join('');
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
    renderNavStatus(health);
  } catch (error) {
    renderNavStatus(null);
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

  if (inference.status === 'capped') {
    dot.className = 'dot error';
    label.textContent = `${name} DAILY CALL CAP REACHED · LOCAL PLANNER`;
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
  // Sections are stacked, so bring the permission card into view (red team buttons sit below it).
  $('manifest').scrollIntoView({ behavior: scrollBehavior(), block: 'nearest' });
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
    $('last-run').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });

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
      // Task output is shown only when the run produced chart data.
      const hasChart = run.result?.artifact?.labels?.length > 0;
      $('chart').closest('.report').classList.toggle('hidden', !hasChart);
      if (hasChart) renderChart(run.result.artifact);
      renderReport(run);
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
  // The table is detail for runs that were allowed websites; the run report covers the rest.
  const show = Boolean(network?.allowed?.length);
  $('network-report').classList.toggle('hidden', !show);
  if (!show) return;
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
        <td>${escapeHtml(a.name).replace(/_/g, '_<wbr>')}</td>
        <td><span class="badge ${a.result === 'OK' ? 'ok' : 'blocked'}">${escapeHtml(a.result)}</span></td>
        <td class="nowrap">${a.as_expected ? '✓ ' + escapeHtml(a.expected) : '<span class="danger">✗ expected ' + escapeHtml(a.expected) + '</span>'}</td>
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
// Overlay navigation ----------------------------------------------------------------------

const nav = { toggle: $('menu-toggle'), overlay: $('site-nav'), closing: null };
const navLinks = () => [...nav.overlay.querySelectorAll('[data-section]')];
const navOpen = () => nav.overlay.classList.contains('open');

function renderNavStatus(health) {
  let provider = 'unknown';
  if (health) {
    provider = health.provider
      ? `${providerLabel(health.provider, health.model)}`
      : 'none configured (built-in planner)';
  }
  $('nav-provider').textContent = provider;
  $('nav-runtime').textContent = health?.runtime || 'unknown';
}

function markActiveLink() {
  // The section whose top is nearest the top of the viewport, among those still on screen.
  let best = navLinks()[0];
  let bestDistance = Infinity;
  for (const link of navLinks()) {
    const rect = $(link.dataset.section).getBoundingClientRect();
    if (rect.bottom <= 80) continue;
    const distance = Math.abs(rect.top);
    if (distance < bestDistance) {
      best = link;
      bestDistance = distance;
    }
  }
  for (const link of navLinks()) {
    link.classList.toggle('active', link === best);
    if (link === best) link.setAttribute('aria-current', 'location');
    else link.removeAttribute('aria-current');
  }
  return best;
}

function openNav() {
  clearTimeout(nav.closing);
  const root = document.documentElement;
  const active = markActiveLink();
  root.style.setProperty('--sbw', `${innerWidth - root.clientWidth}px`);
  root.classList.remove('nav-closing');
  root.classList.add('nav-open');
  nav.overlay.inert = false;
  nav.overlay.setAttribute('aria-hidden', 'false');
  nav.overlay.classList.add('open');
  nav.toggle.setAttribute('aria-expanded', 'true');
  nav.toggle.setAttribute('aria-label', 'Close menu');
  active.focus({ preventScroll: true });
}

function closeNav() {
  if (!navOpen()) return;
  const root = document.documentElement;
  nav.overlay.classList.remove('open');
  nav.overlay.setAttribute('aria-hidden', 'true');
  nav.overlay.inert = true;
  nav.toggle.setAttribute('aria-expanded', 'false');
  nav.toggle.setAttribute('aria-label', 'Open menu');
  root.classList.remove('nav-open');
  // Keep the X on a transparent background while the overlay fades out.
  root.classList.add('nav-closing');
  nav.closing = setTimeout(() => root.classList.remove('nav-closing'), reducedMotion() ? 0 : 250);
  nav.toggle.focus({ preventScroll: true });
}

nav.toggle.addEventListener('click', () => (navOpen() ? closeNav() : openNav()));

for (const link of navLinks()) {
  link.addEventListener('click', (event) => {
    event.preventDefault();
    closeNav();
    $(link.dataset.section).scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
  });
}

document.addEventListener('keydown', (event) => {
  if (!navOpen()) return;
  if (event.key === 'Escape') {
    event.preventDefault();
    closeNav();
  } else if (event.key === 'Tab') {
    // Focus stays on the close control and the overlay's links while it is open.
    const items = [nav.toggle, ...nav.overlay.querySelectorAll('a[href]')];
    const index = items.indexOf(document.activeElement);
    const next = index === -1 ? 0 : (index + (event.shiftKey ? -1 : 1) + items.length) % items.length;
    event.preventDefault();
    items[next].focus();
  }
});

$('hero-cta').addEventListener('click', () => {
  $('run-task').scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
  $('task').focus({ preventScroll: true });
});

// Technical details: the raw attempts, stdout and audit record, collapsed by default.
$('tech-toggle').addEventListener('click', () => {
  const open = $('tech-toggle').getAttribute('aria-expanded') !== 'true';
  $('tech-toggle').setAttribute('aria-expanded', String(open));
  $('tech-details').hidden = !open;
});
