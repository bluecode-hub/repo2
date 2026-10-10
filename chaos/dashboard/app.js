// ================================================================
// tabs
// ================================================================
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('panel-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'graph' && cy) setTimeout(() => cy.resize(), 50);
  });
});

// ================================================================
// health pill
// ================================================================
async function refreshHealth() {
  try {
    const r = await fetch('/api/live');
    const d = await r.json();
    const pill = document.getElementById('health-pill');
    const text = document.getElementById('health-text');
    pill.className = 'status-pill ' + (d.healthy ? 'ok' : 'bad');
    text.textContent = d.healthy ? 'healthy' : (d.reasons[0] || 'unhealthy');
    document.getElementById('updated-at').textContent = new Date().toLocaleTimeString();
  } catch (e) { /* server not up yet */ }
}

// ================================================================
// dependency graph (cytoscape, dagre layout)
// ================================================================
cytoscape.use(cytoscapeDagre);
let cy = null;

const CRIT_COLOR = {
  critical: '#D13B32', high: '#B9790F', medium: '#7E97AF', low: '#9A9183', none: '#9A9183',
};

function initGraph() {
  cy = cytoscape({
    container: document.getElementById('cy'),
    style: [
      { selector: 'node', style: {
          'shape': 'round-rectangle',
          'background-color': '#FFFFFF',
          'border-width': 1.8,
          'border-color': ele => CRIT_COLOR[ele.data('crit') || 'low'],
          'label': 'data(label)',
          'color': '#2A2621',
          'font-family': 'JetBrains Mono, monospace',
          'font-size': 11,
          'font-weight': 600,
          'text-valign': 'center',
          'text-halign': 'center',
          'width': 'label',
          'height': 32,
          'padding': '13px',
          'text-max-width': '140px',
      }},
      { selector: 'node[spof = "true"]', style: {
          'border-style': 'dashed',
          'border-width': 2.3,
          'border-color': '#E8206F',
      }},
      { selector: 'node:selected', style: {
          'background-color': '#FBEFF4',
          'border-width': 2.8,
      }},
      { selector: 'edge', style: {
          'curve-style': 'bezier',
          'target-arrow-shape': 'triangle',
          'target-arrow-color': '#B9AD91',
          'line-color': '#C9BC9D',
          'width': ele => 1.2 + Math.min(ele.data('calls') / 60, 4),
          'opacity': 0.75,
          'label': 'data(calls)',
          'font-family': 'JetBrains Mono, monospace',
          'font-size': 9,
          'font-weight': 600,
          'color': '#8B8168',
          'text-background-color': '#F4EEE1',
          'text-background-opacity': 1,
          'text-background-padding': 2,
      }},
      { selector: 'edge:selected', style: { 'line-color': '#E8206F', 'target-arrow-color': '#E8206F', 'opacity': 1 } },
    ],
    layout: { name: 'preset' },
    wheelSensitivity: 0.25,
  });

  cy.on('tap', 'node', evt => showNodeDetail(evt.target.data()));
  cy.on('tap', evt => { if (evt.target === cy) hideNodeDetail(); });

  // The real bug (confirmed live, twice): cytoscape can compute its first layout before
  // #graph-wrap has its real flex-computed size, rendering everything collapsed into the
  // top-left corner. A requestAnimationFrame delay on first load only reduced how often
  // this showed -- it didn't fix it, because refreshGraph() skips re-running the layout
  // whenever the dependency data hasn't changed since the last poll, so a bad first render
  // never got a second chance to fix itself. A ResizeObserver reacts to the container's
  // ACTUAL size changing (tab switches, window resizes, the browser finishing its first
  // layout pass) and re-fits every time, independent of whether the data changed.
  new ResizeObserver(() => { if (cy && cy.elements().length) { cy.resize(); cy.fit(undefined, 48); } })
    .observe(document.getElementById('graph-wrap'));
}

function showNodeDetail(d) {
  const el = document.getElementById('node-detail');
  const deps = (d.depends_on || []).map(x => `<span class="chip">${x}</span>`).join('') || '<span class="chip">none</span>';
  const dependents = (d.dependents || []).map(x => `<span class="chip">${x}</span>`).join('') || '<span class="chip">none</span>';
  el.innerHTML = `
    <div class="nd-name">${d.label}</div>
    <div class="nd-sub">${d.crit}${d.spof === 'true' ? ' &middot; single point of failure' : ''}</div>
    <div class="nd-row"><span class="k">criticality score</span><span class="v">${d.score}</span></div>
    <div class="nd-row"><span class="k">traffic share</span><span class="v">${d.traffic != null ? (d.traffic*100).toFixed(1)+'%' : '-'}</span></div>
    <div class="nd-row"><span class="k">status (last mapped)</span><span class="v">${d.status}</span></div>
    <div class="nd-deps"><div class="k">depends on</div>${deps}</div>
    <div class="nd-deps"><div class="k">depended on by</div>${dependents}</div>
  `;
  el.classList.add('show');
}
function hideNodeDetail() { document.getElementById('node-detail').classList.remove('show'); }

let lastGraphSignature = '';

async function refreshGraph() {
  const r = await fetch('/api/dependency-map');
  const dep = await r.json();
  const appServices = (dep.services || []).filter(s => s.role === 'app');
  const edges = (dep.edges || []).filter(e =>
    appServices.some(s => s.name === e.from) && appServices.some(s => s.name === e.to));

  const signature = JSON.stringify([appServices.map(s=>s.name), edges.map(e=>[e.from,e.to,e.calls])]);
  document.getElementById('hud-services').textContent = appServices.length;
  document.getElementById('hud-spof').textContent = appServices.filter(s => s.single_point_of_failure).length;
  document.getElementById('hud-edges').textContent = edges.length;

  if (signature === lastGraphSignature) return; // avoid re-layout thrash when nothing changed
  lastGraphSignature = signature;

  const nodes = appServices.map(s => ({
    data: {
      id: s.name, label: s.name, crit: s.declared_criticality || 'low',
      spof: String(s.single_point_of_failure), score: s.criticality_score,
      traffic: s.traffic_share, status: s.status,
      depends_on: s.depends_on, dependents: s.dependents,
    }
  }));
  const eles = edges.map(e => ({ data: { id: e.from + '__' + e.to, source: e.from, target: e.to, calls: e.calls || 0 } }));

  cy.elements().remove();
  cy.add([...nodes, ...eles]);
  // cytoscape can init before #graph-wrap has its real flex-computed size (confirmed live:
  // first load rendered everything collapsed into the top-left corner) -- resize against
  // the container's actual current dimensions before every layout run, not just on tab
  // switch, so this is correct on first paint too.
  cy.resize();
  cy.layout({
    name: 'dagre', rankDir: 'LR', nodeSep: 30, rankSep: 95, edgeSep: 14,
    animate: true, animationDuration: 400, fit: true, padding: 48,
  }).run();
}

// ================================================================
// prometheus insights (structured EDA -- ranked tables, no charts)
// ================================================================
function pct(v) { return (v * 100).toFixed(1) + '%'; }
function ms(v) { return v == null ? '-' : v + 'ms'; }

function rankRows(rows, key, fmt, barClass) {
  const max = Math.max(...rows.map(r => r[key] || 0), 0.0001);
  return rows.map(r => `
    <tr>
      <td>${r.service || r.container}</td>
      <td>
        <div class="rank-bar-wrap">
          <div class="rank-bar-track"><div class="rank-bar-fill ${barClass}" style="width:${Math.max((r[key]||0)/max*100, 2)}%"></div></div>
          <span class="val-num">${fmt(r[key])}</span>
        </div>
      </td>
    </tr>`).join('');
}

function renderInsights(d) {
  const panel = document.getElementById('insights-panel');
  if (d.error || !d.summary.services) {
    panel.innerHTML = `<div class="empty-state">Prometheus not reachable yet${d.error ? ': ' + d.error : ''} -- is the stack up?</div>`;
    return;
  }
  const s = d.summary;

  const devRows = d.baseline_deviation.length ? d.baseline_deviation.map(r => `
    <tr>
      <td>${r.service}</td>
      <td class="val-num">${ms(r.baseline_p95_ms)}</td>
      <td class="val-num">${ms(r.current_p95_ms)}</td>
      <td class="val-num ${r.deviation_pct > 0 ? 'dev-pos' : 'dev-neg'}">${r.deviation_pct > 0 ? '+' : ''}${r.deviation_pct}%</td>
    </tr>`).join('') : `<tr><td colspan="4" style="font-family:var(--sans);color:var(--text-faint)">no baseline.json captured yet -- run monitor.py baseline</td></tr>`;

  panel.innerHTML = `
    <div class="kpi-row">
      <div class="kpi-card"><div class="kpi-val">${s.services}</div><div class="kpi-label">services</div></div>
      <div class="kpi-card"><div class="kpi-val">${s.total_rps}</div><div class="kpi-label">total req/s</div></div>
      <div class="kpi-card"><div class="kpi-val ${s.avg_error_rate > 0 ? 'pink' : ''}">${pct(s.avg_error_rate)}</div><div class="kpi-label">avg error rate</div></div>
      <div class="kpi-card"><div class="kpi-val">${ms(s.avg_p95_ms)}</div><div class="kpi-label">avg p95 latency</div></div>
      <div class="kpi-card"><div class="kpi-val pink">${ms(s.max_p95_ms)}</div><div class="kpi-label">highest p95 latency</div></div>
    </div>

    <div class="insights-grid">
      <div class="insight-card">
        <div class="insight-head">highest latency (p95)<span class="hint">by service</span></div>
        <table class="data-table"><thead><tr><th>service</th><th>p95 ms</th></tr></thead>
          <tbody>${rankRows(d.by_latency, 'p95_ms', ms, 'pink')}</tbody></table>
      </div>

      <div class="insight-card">
        <div class="insight-head">highest error rate<span class="hint">by service</span></div>
        <table class="data-table"><thead><tr><th>service</th><th>error rate</th></tr></thead>
          <tbody>${rankRows(d.by_error_rate, 'error_rate', pct, 'pink')}</tbody></table>
      </div>

      <div class="insight-card">
        <div class="insight-head">highest traffic<span class="hint">req/s, by service</span></div>
        <table class="data-table"><thead><tr><th>service</th><th>req/s</th></tr></thead>
          <tbody>${rankRows(d.by_traffic, 'rps', v => v, 'oat')}</tbody></table>
      </div>

      <div class="insight-card">
        <div class="insight-head">highest memory usage<span class="hint">by container</span></div>
        <table class="data-table"><thead><tr><th>container</th><th>memory</th></tr></thead>
          <tbody>${rankRows(d.by_memory, 'memory_mb', v => v.toFixed(0)+' MB', 'amber')}</tbody></table>
      </div>

      <div class="insight-card">
        <div class="insight-head">deviation from baseline<span class="hint">live p95 vs recorded baseline</span></div>
        <table class="data-table"><thead><tr><th>service</th><th>baseline p95</th><th>live p95</th><th>deviation</th></tr></thead>
          <tbody>${devRows}</tbody></table>
      </div>
    </div>
  `;
}

async function refreshInsights() {
  const r = await fetch('/api/prometheus-insights');
  renderInsights(await r.json());
}

// ================================================================
// agent traces (waterfall)
// ================================================================
function buildSpans(events) {
  const byRun = {};
  for (const e of events) {
    const type = e.event.split('_')[0]; // chain | llm | tool
    if (!byRun[e.run_id]) byRun[e.run_id] = { run_id: e.run_id, parent_run_id: e.parent_run_id, type };
    const span = byRun[e.run_id];
    span.parent_run_id = span.parent_run_id || e.parent_run_id;
    if (e.event.endsWith('_start')) {
      span.name = e.name; span.start_ts = e.ts; span.input = e.input;
    } else {
      span.end_ts = e.ts; span.duration_ms = e.duration_ms; span.status = e.status;
      span.output = e.output; span.error = e.error;
    }
  }
  return Object.values(byRun).filter(s => s.start_ts);
}

function groupIntoTraces(spans) {
  const byId = {}; spans.forEach(s => byId[s.run_id] = s);
  const roots = spans.filter(s => !s.parent_run_id || !byId[s.parent_run_id]);
  roots.sort((a, b) => b.start_ts - a.start_ts);
  const children = id => spans.filter(s => s.parent_run_id === id).sort((a,b) => a.start_ts - b.start_ts);
  return roots.map(root => {
    const all = [];
    (function walk(node, depth) {
      all.push({ ...node, depth });
      children(node.run_id).forEach(c => walk(c, depth + 1));
    })(root, 0);
    const ends = all.map(s => s.end_ts || s.start_ts);
    const totalEnd = Math.max(...ends);
    const totalStart = Math.min(...all.map(s => s.start_ts));
    return { root, rows: all, totalStart, totalEnd: Math.max(totalEnd, totalStart + 0.001) };
  });
}

function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleTimeString() : '-'; }

function renderTraces(traces) {
  const panel = document.getElementById('traces-panel');
  document.getElementById('trace-count').textContent = traces.reduce((n, t) => n + t.rows.length, 0);

  const note = `<div class="trace-note"><span class="pill-mini">local &middot; self-hosted</span>
    <span>captures every chain / LLM / tool call via a LangChain callback handler -- the same data LangSmith shows, stored on disk, zero external calls. LangSmith can run alongside this if you add <code>LANGCHAIN_TRACING_V2=true</code> and a key -- it's additive, not a replacement.</span></div>`;

  if (!traces.length) {
    panel.innerHTML = note + `<div class="empty-state">no agent runs yet -- run demo_agent.py (or any graph.invoke(..., config={"callbacks":[tracer]}))</div>`;
    return;
  }

  panel.innerHTML = note + traces.map((t, ti) => {
    const span = Math.max(t.totalEnd - t.totalStart, 0.001);
    const rows = t.rows.map((r, ri) => {
      const offsetPct = ((r.start_ts - t.totalStart) / span) * 100;
      const widthPct = Math.max(((r.end_ts ? r.end_ts - r.start_ts : 0) / span) * 100, 0.6);
      const errCls = r.status === 'error' ? ' err' : '';
      const detail = r.status === 'error' ? (r.error || '') : (r.output || r.input || '');
      const rid = `d-${ti}-${ri}`;
      return `
        <div class="trace-row${errCls}">
          <div class="trace-row-label" onclick="document.getElementById('${rid}').classList.toggle('show')" style="margin-left:${r.depth*16}px">
            <span class="trace-type-dot ${r.type}"></span>${r.name || r.type}
          </div>
          <div class="trace-bar-track">
            <div class="trace-bar-fill ${r.status === 'error' ? 'error' : r.type}" style="left:${offsetPct}%;width:${widthPct}%"></div>
          </div>
          <div class="trace-dur">${r.duration_ms != null ? r.duration_ms + 'ms' : '...'}</div>
        </div>
        <div class="trace-detail-body" id="${rid}">${(detail+'').slice(0, 800)}</div>
      `;
    }).join('');
    return `
      <div class="trace-run">
        <div class="trace-run-head">
          <div class="trace-run-name">${t.root.name || 'agent run'}</div>
          <div class="trace-run-meta">${fmtTime(t.root.start_ts)} &middot; ${t.rows.length} spans &middot; ${Math.round(span*1000)}ms</div>
        </div>
        <div class="trace-rows">${rows}</div>
      </div>`;
  }).join('');
}

async function refreshTraces() {
  const r = await fetch('/api/agent-traces?limit=500');
  const events = await r.json();
  const spans = buildSpans(events);
  const traces = groupIntoTraces(spans);
  renderTraces(traces);
}

// ================================================================
// baseline vs injection (tab 4) -- Day-1 "impact visible" requirement
// ================================================================
function pct1(v) { return v == null ? '-' : (v * 100).toFixed(1) + '%'; }
function ms0(v) { return v == null ? '-' : Math.round(v) + 'ms'; }

function renderImpact(rows) {
  const panel = document.getElementById('impact-panel');
  const baseline = [...rows].reverse().find(r => r.type === 'baseline');
  const injections = rows.filter(r => r.type === 'injection');
  document.getElementById('impact-count').textContent = injections.length;

  if (!baseline) {
    panel.innerHTML = `<div id="impact-inner"><div class="empty-state">no baseline captured yet -- run day1_pipeline.py</div></div>`;
    return;
  }

  const bSnap = baseline.snapshot;
  const baselineHtml = `
    <div class="baseline-card${baseline.healthy ? '' : ' degraded'}">
      <div class="bc-label"><span class="bc-dot"></span>baseline conditions &middot; captured ${fmtTime(baseline.ts)} &middot; before any fault</div>
      ${baseline.healthy ? '' : `<div class="bc-warning">captured while the system was NOT at steady state (${(baseline.reasons || []).join('; ')}) -- retried 4 times first, this is a real reading, not a glitch. Treat the comparison below cautiously: "before" wasn't clean.</div>`}
      <div class="bc-stats">
        <div><div class="bc-stat-val">${pct1(bSnap.probe_error_rate)}</div><div class="bc-stat-label">probe error rate</div></div>
        <div><div class="bc-stat-val">${ms0(bSnap.probe_max_ms)}</div><div class="bc-stat-label">slowest probe</div></div>
        <div><div class="bc-stat-val">${Object.keys(bSnap.services || {}).length}</div><div class="bc-stat-label">services tracked</div></div>
        <div><div class="bc-stat-val">${baseline.healthy ? 'healthy' : 'degraded'}</div><div class="bc-stat-label">steady state</div></div>
      </div>
    </div>`;

  if (!injections.length) {
    panel.innerHTML = `<div id="impact-inner">${baselineHtml}<div class="empty-state">baseline stored -- waiting for the first injection to run</div></div>`;
    return;
  }

  const cards = injections.map(inj => {
    const cls = inj.aborted ? 'aborted' : 'completed';
    const b = inj.before, a = inj.after;
    const bSvc = b.service || {}, aSvc = a.service || {};
    return `
      <div class="injection-card ${cls}">
        <div class="ic-head">
          <div class="ic-title">${inj.target} <span style="color:var(--text-faint);font-weight:400">/</span> ${inj.fault}</div>
          <div style="display:flex;align-items:center;gap:10px">
            <span class="ic-badge ${cls}">${inj.aborted ? 'kill switch fired' : 'completed'}</span>
            <span class="ic-time">${fmtTime(inj.ts)}</span>
          </div>
        </div>
        <div class="ic-reason">${inj.reason || ''}</div>
        <div class="ic-compare">
          <div class="ic-col">
            <div class="ic-col-label">before (baseline)</div>
            <div class="ic-metric"><span class="k">probe error rate</span><span class="v">${pct1(b.probe_error_rate)}</span></div>
            <div class="ic-metric"><span class="k">slowest probe</span><span class="v">${ms0(b.probe_max_ms)}</span></div>
            <div class="ic-metric"><span class="k">${inj.target} req/s</span><span class="v">${bSvc.rps ?? '-'}</span></div>
            <div class="ic-metric"><span class="k">${inj.target} p95</span><span class="v">${ms0(bSvc.p95_ms)}</span></div>
          </div>
          <div class="ic-col">
            <div class="ic-col-label">after (peak impact)</div>
            <div class="ic-metric"><span class="k">probe error rate</span><span class="v ${a.probe_error_rate > b.probe_error_rate ? 'bad' : ''}">${pct1(a.probe_error_rate)}</span></div>
            <div class="ic-metric"><span class="k">slowest probe</span><span class="v ${a.probe_max_ms > b.probe_max_ms ? 'bad' : ''}">${ms0(a.probe_max_ms)}</span></div>
            <div class="ic-metric"><span class="k">${inj.target} req/s</span><span class="v">${aSvc.rps ?? '-'}</span></div>
            <div class="ic-metric"><span class="k">${inj.target} p95</span><span class="v ${(aSvc.p95_ms||0) > (bSvc.p95_ms||0) ? 'bad' : ''}">${ms0(aSvc.p95_ms)}</span></div>
          </div>
        </div>
        <div class="ic-explanation"><div class="ic-exp-label">what the agent observed</div>${inj.explanation}</div>
        <div class="ic-recovery">recovery: ${inj.recovery_sec != null ? inj.recovery_sec + 's' : 'not recovered within timeout'}${inj.abort_reason ? ' &middot; kill switch reason: ' + inj.abort_reason : ''}</div>
      </div>`;
  }).join('');

  panel.innerHTML = `<div id="impact-inner">${baselineHtml}${cards}</div>`;
}

async function refreshImpact() {
  const r = await fetch('/api/injection-comparison');
  renderImpact(await r.json());
}

// ---------------- manual target/fault controls ----------------
async function loadTargetOptions() {
  const r = await fetch('/api/available-targets');
  const d = await r.json();
  const sel = document.getElementById('target-select');
  sel.innerHTML = d.targets.map(t =>
    `<option value="${t.name}">${t.name} (score ${t.criticality_score}${t.single_point_of_failure ? ', SPOF' : ''}${t.already_tested ? ', already tested' : ''})</option>`
  ).join('');
}

function setRunStatus(text, cls) {
  const el = document.getElementById('run-status');
  el.textContent = text;
  el.className = 'run-status' + (cls ? ' ' + cls : '');
}

async function pollPipelineStatus() {
  const r = await fetch('/api/pipeline-status');
  const d = await r.json();
  document.getElementById('run-btn').disabled = d.running;
  document.getElementById('reset-btn').disabled = d.running;
  if (d.running) setRunStatus('experiment running -- watch tab 5 for live progress', 'running');
  else if (document.getElementById('run-status').classList.contains('running')) setRunStatus('done -- check the card below');
  return d.running;
}

document.getElementById('run-btn').addEventListener('click', async () => {
  const target = document.getElementById('target-select').value;
  const fault = document.getElementById('fault-select').value;
  if (!target) { setRunStatus('pick a target first', 'error'); return; }
  const r = await fetch('/api/run-experiment', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ target, fault: fault || undefined }),
  });
  const d = await r.json();
  if (d.status === 'error') setRunStatus(d.message, 'error');
  else setRunStatus(`started: ${d.target} / ${d.fault || 'auto'}`, 'running');
});

document.getElementById('reset-btn').addEventListener('click', async () => {
  if (!confirm('Clear the experiment history (baseline + all injection rows) for a clean slate? The dependency map and statistical baseline are not affected.')) return;
  const r = await fetch('/api/reset', { method: 'POST' });
  const d = await r.json();
  setRunStatus(d.status === 'cleared' ? 'cleared -- ready for a fresh baseline' : d.message,
               d.status === 'cleared' ? '' : 'error');
  refreshImpact();
});

// ================================================================
// live log (tab 5) -- scrolling terminal
// ================================================================
let lastLogCount = 0;

function renderLogs(lines) {
  const panel = document.getElementById('logs-panel');
  if (!lines.length) {
    panel.innerHTML = `<div style="color:#5c6370">waiting for the pipeline to start -- run day1_pipeline.py</div><span class="log-cursor"></span>`;
    return;
  }
  const wasAtBottom = panel.scrollTop + panel.clientHeight >= panel.scrollHeight - 30;
  panel.innerHTML = lines.map(l => `
    <div class="log-line">
      <span class="log-ts">${new Date(l.ts * 1000).toLocaleTimeString()}</span>
      <span class="log-level ${l.level}">${l.level.toUpperCase()}</span>
      <span class="log-msg">${l.message}</span>
    </div>`).join('') + `<span class="log-cursor"></span>`;
  if (wasAtBottom || lines.length !== lastLogCount) panel.scrollTop = panel.scrollHeight;
  lastLogCount = lines.length;
}

async function refreshLogs() {
  const r = await fetch('/api/live-log?limit=1000');
  renderLogs(await r.json());
}

// ================================================================
// boot
// ================================================================
initGraph();
refreshHealth();
// Defer the first graph layout a frame past initial paint: cytoscape can read the
// container's size before the browser's finished the first layout pass, which produced
// a visibly broken cramped-corner render on load (confirmed live) that only self-corrected
// on the next 4s poll -- too slow for anyone watching at page load.
requestAnimationFrame(() => requestAnimationFrame(refreshGraph));
refreshInsights();
refreshTraces();
refreshImpact();
refreshLogs();
loadTargetOptions();
pollPipelineStatus();
setInterval(refreshHealth, 2000);
setInterval(refreshGraph, 4000);
setInterval(refreshInsights, 5000);
setInterval(refreshTraces, 3000);
setInterval(refreshImpact, 2500);
setInterval(refreshLogs, 1500);
setInterval(loadTargetOptions, 6000);
setInterval(pollPipelineStatus, 2000);
