"""Self-contained dark-mode VFX studio HTML templates for dashboard and static report."""

from __future__ import annotations

_SHARED_CSS = """
:root {
  --bg: #0b0f17;
  --panel: #111827;
  --panel-2: #172033;
  --border: #263248;
  --text: #e5edf7;
  --muted: #94a3b8;
  --accent: #38bdf8;
  --pass: #22c55e;
  --fail: #ef4444;
  --warn: #f59e0b;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  --sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: radial-gradient(circle at top right, #131d33 0%, var(--bg) 52%);
  color: var(--text);
  font-family: var(--sans);
  line-height: 1.45;
}
.mono { font-family: var(--mono); }
.badge {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-transform: uppercase;
  border: 1px solid transparent;
}
.badge-pass { background: rgba(34,197,94,0.14); color: #4ade80; border-color: rgba(34,197,94,0.35); }
.badge-fail { background: rgba(239,68,68,0.14); color: #f87171; border-color: rgba(239,68,68,0.35); }
.badge-warn { background: rgba(245,158,11,0.14); color: #fbbf24; border-color: rgba(245,158,11,0.35); }
.badge-info { background: rgba(56,189,248,0.14); color: #38bdf8; border-color: rgba(56,189,248,0.35); }
.card {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 16px;
}
.btn {
  appearance: none;
  background: var(--panel-2);
  color: var(--text);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 7px 13px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.14s ease;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.btn:hover:not(:disabled) { border-color: var(--accent); background: #1e293b; }
.btn:disabled { opacity: 0.5; cursor: wait; }
.btn-primary {
  background: linear-gradient(135deg, #0284c7, #2563eb);
  border-color: #38bdf8;
  color: #fff;
}
.btn-primary:hover:not(:disabled) { filter: brightness(1.1); }
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
}
.kpi {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px 14px;
}
.kpi-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; }
.kpi-val { font-size: 24px; font-weight: 700; margin-top: 2px; }
table.checks {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
table.checks th, table.checks td {
  text-align: left;
  padding: 8px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
}
table.checks th {
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.wipe-wrap {
  position: relative;
  width: 100%;
  aspect-ratio: 16 / 9;
  background: #05080f;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--border);
  user-select: none;
}
.wipe-wrap img {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: contain;
}
.wipe-top { clip-path: inset(0 50% 0 0); }
.wipe-divider {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 50%;
  width: 2px;
  background: var(--accent);
  box-shadow: 0 0 10px rgba(56,189,248,0.8);
  pointer-events: none;
}
.wipe-label {
  position: absolute;
  top: 10px;
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(11,15,23,0.82);
  border: 1px solid var(--border);
  font-size: 11px;
  font-weight: 600;
}
.wipe-label.left { left: 10px; }
.wipe-label.right { right: 10px; }
.wipe-slider { width: 100%; margin-top: 8px; accent-color: var(--accent); }
.meter-bar {
  height: 8px;
  background: #0a0f1a;
  border-radius: 999px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.meter-fill { height: 100%; border-radius: 999px; transition: width 0.2s ease; }
pre.codebox {
  background: #070b12;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  font-family: var(--mono);
  font-size: 12px;
  overflow-x: auto;
  color: #cbd5e1;
  margin: 0;
}
"""

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ComfyGuard — Pipeline Gatekeeper Console</title>
<style>
__SHARED_CSS__
.topbar {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 14px 22px;
  border-bottom: 1px solid var(--border);
  background: rgba(11,15,23,0.88);
  backdrop-filter: blur(8px);
  position: sticky;
  top: 0;
  z-index: 20;
}
.brand { display: flex; align-items: center; gap: 12px; }
.logo {
  width: 34px; height: 34px; border-radius: 9px;
  background: linear-gradient(135deg, #0ea5e9, #2563eb);
  display: grid; place-items: center;
  font-weight: 800; font-size: 15px; color: #fff;
}
.actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.layout {
  display: grid;
  grid-template-columns: 330px 1fr;
  gap: 16px;
  padding: 16px 22px 28px;
  max-width: 1600px;
  margin: 0 auto;
}
@media (max-width: 1020px) { .layout { grid-template-columns: 1fr; } }
.sidebar { display: flex; flex-direction: column; gap: 14px; }
.job-item {
  width: 100%;
  text-align: left;
  background: var(--panel-2);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 11px 12px;
  color: var(--text);
  cursor: pointer;
  transition: border-color 0.14s ease;
}
.job-item:hover, .job-item.active { border-color: var(--accent); background: #1b273d; }
.tabs { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.tab {
  padding: 7px 14px; border-radius: 8px; border: 1px solid var(--border);
  background: var(--panel-2); color: var(--muted); font-size: 13px; font-weight: 600;
  cursor: pointer;
}
.tab.active { color: #fff; border-color: var(--accent); background: #1e3a5f; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
.frame-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.pill {
  padding: 4px 10px; border-radius: 7px; font-size: 12px; font-family: var(--mono);
  background: var(--panel-2); border: 1px solid var(--border); color: var(--text); cursor: pointer;
}
.pill.active { border-color: var(--accent); background: #1d3557; }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); }
.dot { width: 10px; height: 10px; border-radius: 99px; display: inline-block; margin-right: 4px; }
.form-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 10px;
  margin-bottom: 12px;
}
.field label { display: block; font-size: 11px; color: var(--muted); margin-bottom: 4px; }
.field input, .field select {
  width: 100%; background: #090e18; color: var(--text); border: 1px solid var(--border);
  border-radius: 7px; padding: 6px 9px; font-family: var(--mono); font-size: 13px;
}
.worker-row {
  display: flex; justify-content: space-between; align-items: center;
  padding: 8px 10px; border-radius: 8px; background: var(--panel-2);
  border: 1px solid var(--border); font-size: 12px; margin-top: 8px;
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo">CG</div>
    <div>
      <div style="font-weight:700;font-size:16px;">ComfyGuard Pipeline Console</div>
      <div style="font-size:12px;color:var(--muted);">
        Deterministic AI Pass Broker &amp; Contract Gatekeeper
      </div>
    </div>
  </div>
  <div class="actions">
    <button class="btn btn-primary" id="btn-run-all" onclick="runDemo('all')">
      Run All 3 Scenarios
    </button>
    <button class="btn" onclick="runDemo('1')">S1: 1080p Reject</button>
    <button class="btn" onclick="runDemo('2')">S2: Drift Quarantine</button>
    <button class="btn" onclick="runDemo('3')">S3: Clean Publish</button>
    <button class="btn" onclick="rescanWorkspace()">Rescan Disk</button>
    <a class="btn" href="/api/report" download="comfyguard_report.html">Export HTML Report</a>
  </div>
</header>

<div style="padding:14px 22px 0;max-width:1600px;margin:0 auto;">
  <div class="kpi-grid" id="kpis"></div>
</div>

<main class="layout">
  <aside class="sidebar">
    <section class="card">
      <div style="font-weight:700;font-size:13px;margin-bottom:4px;">Worker Pool &amp; Queue</div>
      <div style="font-size:12px;color:var(--muted);">
        Pre/Post-Flight Gatekeeper Endpoints
      </div>
      <div id="workers-list"></div>
    </section>

    <section class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
        <div style="font-weight:700;font-size:13px;">Submission Queue</div>
        <span class="mono" style="font-size:11px;color:var(--muted);" id="job-count">0 jobs</span>
      </div>
      <div id="jobs-list" style="display:flex;flex-direction:column;gap:8px;"></div>
    </section>
  </aside>

  <section class="card" id="detail-pane">
    <div style="color:var(--muted);padding:32px;text-align:center;">
      Click <strong>Run All 3 Scenarios</strong> above to execute the live mock-worker pipeline
      demonstration, or inspect a discovered workspace job on the left.
    </div>
  </section>
</main>

<script>
let appState = null;
let selectedJobId = null;
let selectedJob = null;
let selectedFrameIdx = 0;
let activeTab = 'visual';

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function statusBadge(status) {
  if (status === 'PUBLISHED' || status === 'published' || status === 'pass') {
    return '<span class="badge badge-pass">' + esc(status) + '</span>';
  }
  if (status === 'SKIP' || status === 'skip' || status === 'WARN' || status === 'warn') {
    return '<span class="badge badge-warn">' + esc(status) + '</span>';
  }
  return '<span class="badge badge-fail">' + esc(status) + '</span>';
}

async function fetchState() {
  const res = await fetch('/api/state');
  appState = await res.json();
  renderSidebarAndKPIs();
  if (!selectedJobId && appState.jobs.length > 0) {
    await selectJob(appState.jobs[0].job_id);
  } else if (selectedJobId) {
    const stillExists = appState.jobs.some(j => j.job_id === selectedJobId);
    if (stillExists) await selectJob(selectedJobId);
  }
}

function renderSidebarAndKPIs() {
  const k = appState.kpis;
  document.getElementById('kpis').innerHTML = [
    ['Total Jobs', k.total_jobs, 'var(--text)'],
    ['Published EXRs', k.published_frames, 'var(--pass)'],
    ['Quarantined Frames', k.quarantined_frames, 'var(--warn)'],
    ['Pre-Flight Blocked', k.preflight_rejected, 'var(--fail)'],
    ['GPU Prompts Saved', k.gpu_prompts_saved, 'var(--accent)'],
  ].map(([label, val, color]) => `
    <div class="kpi">
      <div class="kpi-label">${label}</div>
      <div class="kpi-val" style="color:${color}">${val}</div>
    </div>
  `).join('');

  const workersHtml = appState.workers.map(w => `
    <div class="worker-row">
      <div>
        <div style="font-weight:600;">${esc(w.name)}</div>
        <div class="mono" style="font-size:11px;color:var(--muted);">${esc(w.url)}</div>
      </div>
      <span class="badge ${w.online ? 'badge-pass' : 'badge-info'}">${esc(w.mode)}</span>
    </div>
  `).join('');
  document.getElementById('workers-list').innerHTML = workersHtml;

  document.getElementById('job-count').textContent = appState.jobs.length + ' jobs';
  const jobsEl = document.getElementById('jobs-list');
  if (appState.jobs.length === 0) {
    jobsEl.innerHTML = '<div style="font-size:12px;color:var(--muted);">No jobs yet.</div>';
    return;
  }
  jobsEl.innerHTML = appState.jobs.map(j => `
    <button class="job-item ${j.job_id === selectedJobId ? 'active' : ''}"
            onclick="selectJob('${esc(j.job_id)}')">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
        <span class="badge badge-info">${esc(j.scenario_tag)}</span>
        ${statusBadge(j.status)}
      </div>
      <div style="font-weight:600;font-size:13px;margin-top:6px;">${esc(j.title)}</div>
      <div class="mono" style="font-size:11px;color:var(--muted);margin-top:2px;">
        ${esc(j.shot_label)} &bull; ${esc(j.pass_label)}
      </div>
    </button>
  `).join('');
}

async function selectJob(jobId) {
  selectedJobId = jobId;
  selectedFrameIdx = 0;
  const res = await fetch('/api/jobs/' + encodeURIComponent(jobId));
  selectedJob = await res.json();
  renderSidebarAndKPIs();
  renderDetail();
}

function setTab(tabName) {
  activeTab = tabName;
  renderDetail();
}

function setFrame(idx) {
  selectedFrameIdx = idx;
  renderDetail();
}

function updateWipe(val) {
  const top = document.getElementById('wipe-top-img');
  const div = document.getElementById('wipe-div');
  if (top && div) {
    const rightPct = 100 - Number(val);
    top.style.clipPath = `inset(0 ${rightPct}% 0 0)`;
    div.style.left = val + '%';
  }
}

function renderDetail() {
  if (!selectedJob) return;
  const j = selectedJob;
  const frame = j.frames[selectedFrameIdx] || j.frames[0];

  const headerHtml = `
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;
                padding-bottom:14px;border-bottom:1px solid var(--border);margin-bottom:14px;">
      <div>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <h2 style="margin:0;font-size:18px;">${esc(j.title)}</h2>
          ${statusBadge(j.status)}
        </div>
        <div class="mono" style="font-size:12px;color:var(--muted);margin-top:4px;">
          job_id=${esc(j.job_id)} | ${esc(j.shot_label)} | ${esc(j.pass_label)} |
          colorspace=${esc(j.colorspace)} | dcc=${esc(j.dcc_source)}
        </div>
      </div>
      <div class="mono" style="font-size:12px;color:var(--muted);text-align:right;">
        <div>Contract: ${esc(j.contract_label)}</div>
        <div>GPU Dispatched: ${j.gpu_prompts_dispatched}</div>
      </div>
    </div>
    <div class="tabs">
      <button class="tab ${activeTab === 'visual' ? 'active' : ''}"
              onclick="setTab('visual')">Visual Gate &amp; Edge Overlay</button>
      <button class="tab ${activeTab === 'gates' ? 'active' : ''}"
              onclick="setTab('gates')">Pre / Post-Flight Checks</button>
      <button class="tab ${activeTab === 'lineage' ? 'active' : ''}"
              onclick="setTab('lineage')">EXR Stamps &amp; Lineage</button>
      <button class="tab ${activeTab === 'diff' ? 'active' : ''}"
              onclick="setTab('diff')">Graph Diff Sandbox</button>
    </div>
  `;

  let bodyHtml = '';
  if (activeTab === 'visual') {
    bodyHtml = renderVisualTab(j, frame);
  } else if (activeTab === 'gates') {
    bodyHtml = renderGatesTab(j);
  } else if (activeTab === 'lineage') {
    bodyHtml = renderLineageTab(j, frame);
  } else {
    bodyHtml = renderDiffTab();
  }

  document.getElementById('detail-pane').innerHTML = headerHtml + bodyHtml;
}

function renderVisualTab(j, f) {
  if (!f) return '<div style="color:var(--muted);">No frames recorded.</div>';
  const pills = j.frames.map((fr, idx) => `
    <button class="pill ${idx === selectedFrameIdx ? 'active' : ''}"
            onclick="setFrame(${idx})">
      f${fr.frame} (${fr.status})
    </button>
  `).join('');

  const leftSrc = f.input_preview || f.output_preview || '';
  const rightSrc = f.output_preview || f.input_preview || '';
  const iouPct = f.edge_iou != null ? Math.round(f.edge_iou * 1000) / 10 : null;
  const iouColor = f.edge_iou == null ? 'var(--accent)' : (
    f.edge_iou >= 0.85 ? 'var(--pass)' : 'var(--fail)'
  );

  return `
    <div class="frame-pills">${pills}</div>
    ${f.quarantine_reason ? `
      <div style="background:rgba(239,68,68,0.12);border:1px solid rgba(239,68,68,0.4);
                  border-radius:8px;padding:10px 12px;margin-bottom:14px;font-size:13px;">
        <strong>Gatekeeper Reason:</strong> ${esc(f.quarantine_reason)}
      </div>` : ''}
    <div class="grid-2">
      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:6px;font-size:13px;">
          <strong>Before / After Wipe</strong>
          <span class="mono" style="color:var(--muted);">${esc(f.resolution)}</span>
        </div>
        <div class="wipe-wrap">
          <img src="${esc(rightSrc)}" alt="Output" />
          <img src="${esc(leftSrc)}" id="wipe-top-img" class="wipe-top" alt="Input" />
          <div class="wipe-divider" id="wipe-div"></div>
          <span class="wipe-label left">Input Beauty</span>
          <span class="wipe-label right">${f.output_preview ? 'AI Output' : 'Blocked'}</span>
        </div>
        <input class="wipe-slider" type="range" min="0" max="100" value="50"
               oninput="updateWipe(this.value)" />
      </div>

      <div>
        <div style="display:flex;justify-content:space-between;margin-bottom:6px;font-size:13px;">
          <strong>Sobel Edge-Drift Diagnostic</strong>
          <span class="mono" style="color:${iouColor};">
            ${iouPct != null ? 'IoU ' + iouPct.toFixed(1) + '% (min 85%)' : 'No Edge Check'}
          </span>
        </div>
        <div class="wipe-wrap">
          ${f.edge_overlay
            ? `<img src="${esc(f.edge_overlay)}" alt="Sobel Edge Overlay" />`
            : `<img src="${esc(leftSrc)}" alt="Input Plate" />`}
        </div>
        <div class="legend" style="margin-top:8px;">
          <span><i class="dot" style="background:#22c55e;"></i>Matched Edge</span>
          <span><i class="dot" style="background:#38bdf8;"></i>Missing CG Edge</span>
          <span><i class="dot" style="background:#ef4444;"></i>Drifted AI Edge</span>
        </div>
      </div>
    </div>

    <div class="grid-2" style="margin-top:14px;">
      <div class="card" style="background:var(--panel-2);">
        <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">
          Structural Edge IoU
        </div>
        <div class="meter-bar">
          <div class="meter-fill" style="width:${iouPct ?? 0}%;background:${iouColor};"></div>
        </div>
        <div class="mono" style="font-size:12px;margin-top:6px;">
          seed=${f.seed} | graph=${esc(f.graph_sha256.slice(0, 16))}...
        </div>
      </div>
      <div class="card" style="background:var(--panel-2);">
        <div style="font-size:12px;color:var(--muted);margin-bottom:6px;">
          Inter-Frame Temporal Delta (max 0.20)
        </div>
        <div class="mono" style="font-size:14px;">
          ${f.temporal_delta != null ? f.temporal_delta.toFixed(5) : 'N/A (single/first frame)'}
        </div>
        ${f.output_file ? `<div class="mono" style="font-size:11px;color:var(--muted);margin-top:4px;word-break:break-all;">${esc(f.output_file)}</div>` : ''}
      </div>
    </div>
  `;
}

function renderCheckTable(title, report) {
  if (!report) return '';
  const rows = (report.checks || []).map(c => `
    <tr>
      <td>${statusBadge(c.status)}</td>
      <td class="mono">${esc(c.check_name)}${c.frame != null ? ' [f' + c.frame + ']' : ''}</td>
      <td>${esc(c.message)}</td>
    </tr>
  `).join('');
  return `
    <div style="margin-bottom:16px;">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <strong>${esc(title)}</strong>
        ${statusBadge(report.passed ? 'pass' : 'fail')}
      </div>
      <table class="checks">
        <thead><tr><th>Status</th><th>Check</th><th>Message</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function renderGatesTab(j) {
  return (
    renderCheckTable('Pre-Flight Validation Gate', j.preflight_report) +
    renderCheckTable('Post-Flight Quality Gate', j.postflight_report)
  );
}

function renderLineageTab(j, f) {
  const attrs = f ? Object.entries(f.exr_attributes || {}) : [];
  const attrRows = attrs.length
    ? attrs.map(([k, v]) => `<tr><td class="mono">${esc(k)}</td><td class="mono">${esc(v)}</td></tr>`).join('')
    : '<tr><td colspan="2" style="color:var(--muted);">No stamped EXR attributes (frame was blocked/quarantined).</td></tr>';

  const rezHtml = (j.rez_packages || []).map(p => `<span class="badge badge-info">${esc(p)}</span>`).join(' ');
  const manifestJson = j.manifest ? JSON.stringify(j.manifest, null, 2) : 'No manifest written (blocked at pre-flight).';

  return `
    <div style="margin-bottom:12px;display:flex;flex-wrap:wrap;gap:8px;align-items:center;">
      <span style="font-size:12px;color:var(--muted);">Rez Environment:</span>
      ${rezHtml || '<span style="font-size:12px;color:var(--muted);">none</span>'}
    </div>
    <div style="margin-bottom:16px;">
      <strong style="display:block;margin-bottom:6px;">
        Stamped OpenEXR Header Attributes (Frame ${f ? f.frame : '-'})
      </strong>
      <table class="checks">
        <thead><tr><th>Attribute Key</th><th>Value</th></tr></thead>
        <tbody>${attrRows}</tbody>
      </table>
    </div>
    <div>
      <strong style="display:block;margin-bottom:6px;">LineageManifest JSON</strong>
      <pre class="codebox" style="max-height:360px;">${esc(manifestJson)}</pre>
    </div>
  `;
}

function renderDiffTab() {
  const diff = appState && appState.latest_diff;
  const diffText = diff ? diff.human_text : 'Run a demo or edit parameters below to compute a contract-validated graph diff.';
  return `
    <div style="margin-bottom:14px;">
      <strong style="display:block;margin-bottom:6px;">
        Interactive Contract &amp; Graph Diff Sandbox (v001 Baseline)
      </strong>
      <div style="font-size:12px;color:var(--muted);margin-bottom:10px;">
        Test parameter overrides against <code>contract.yaml</code> bounds (e.g., try
        <code>denoise = 0.99</code> to trigger a contract rejection).
      </div>
      <div class="form-grid">
        <div class="field"><label>steps (4..60)</label><input id="df-steps" type="number" value="28" /></div>
        <div class="field"><label>cfg (1.0..15.0)</label><input id="df-cfg" type="number" step="0.5" value="7.0" /></div>
        <div class="field"><label>denoise (0.05..0.85)</label><input id="df-denoise" type="number" step="0.05" value="0.45" /></div>
        <div class="field"><label>controlnet_strength (0..1)</label><input id="df-cn" type="number" step="0.05" value="0.85" /></div>
        <div class="field"><label>sampler_name</label>
          <select id="df-sampler">
            <option value="dpmpp_2m">dpmpp_2m</option>
            <option value="euler">euler</option>
            <option value="euler_ancestral">euler_ancestral</option>
            <option value="dpmpp_sde">dpmpp_sde</option>
          </select>
        </div>
      </div>
      <button class="btn btn-primary" onclick="submitDiff()">Compute Diff &amp; Validate</button>
      <div id="diff-error" style="margin-top:10px;"></div>
    </div>
    <div>
      <strong style="display:block;margin-bottom:6px;">Canonical Graph Diff Output</strong>
      <pre class="codebox" id="diff-output">${esc(diffText)}</pre>
    </div>
  `;
}

async function submitDiff() {
  const errEl = document.getElementById('diff-error');
  const outEl = document.getElementById('diff-output');
  const params = {
    steps: Number(document.getElementById('df-steps').value),
    cfg: Number(document.getElementById('df-cfg').value),
    denoise: Number(document.getElementById('df-denoise').value),
    controlnet_strength: Number(document.getElementById('df-cn').value),
    sampler_name: document.getElementById('df-sampler').value,
  };
  const res = await fetch('/api/diff', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ params }),
  });
  const data = await res.json();
  if (!res.ok) {
    errEl.innerHTML = '<div class="badge badge-fail">CONTRACT VIOLATION: ' + esc(data.error) + '</div>';
    return;
  }
  errEl.innerHTML = '<div class="badge badge-pass">CONTRACT VALIDATED (v2 sha=' + esc(data.v2_sha256.slice(0, 12)) + ')</div>';
  appState.latest_diff = data;
  outEl.textContent = data.human_text;
}

async function runDemo(scenario) {
  const btns = document.querySelectorAll('.actions button');
  btns.forEach(b => b.disabled = true);
  try {
    const res = await fetch('/api/demo/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scenario }),
    });
    const data = await res.json();
    if (data.latest_job_id) selectedJobId = data.latest_job_id;
    await fetchState();
  } finally {
    btns.forEach(b => b.disabled = false);
  }
}

async function rescanWorkspace() {
  await fetch('/api/rescan', { method: 'POST' });
  await fetchState();
}

fetchState();
</script>
</body>
</html>
""".replace("__SHARED_CSS__", _SHARED_CSS)

STATIC_REPORT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>ComfyGuard — Visual Pipeline Report</title>
<style>
__SHARED_CSS__
.wrap { max-width: 1380px; margin: 0 auto; padding: 22px; }
.scenario-card { margin-top: 20px; }
.grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }
.legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: var(--muted); }
.dot { width: 10px; height: 10px; border-radius: 99px; display: inline-block; margin-right: 4px; }
</style>
</head>
<body>
<div class="wrap" id="root"></div>
<script>
const REPORT = __REPORT_DATA_JSON__;

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function badge(status) {
  if (status === 'PUBLISHED' || status === 'published' || status === 'pass') {
    return '<span class="badge badge-pass">' + esc(status) + '</span>';
  }
  if (status === 'SKIP' || status === 'skip' || status === 'WARN' || status === 'warn') {
    return '<span class="badge badge-warn">' + esc(status) + '</span>';
  }
  return '<span class="badge badge-fail">' + esc(status) + '</span>';
}

function moveWipe(idx, val) {
  const top = document.getElementById('wtop-' + idx);
  const div = document.getElementById('wdiv-' + idx);
  if (top && div) {
    top.style.clipPath = `inset(0 ${100 - Number(val)}% 0 0)`;
    div.style.left = val + '%';
  }
}

(function render() {
  const jobs = REPORT.jobs || [];
  let pub = 0, quar = 0, preBlocked = 0;
  jobs.forEach(j => {
    if (j.status === 'PREFLIGHT_REJECTED') preBlocked++;
    (j.frames || []).forEach(f => {
      if (f.status === 'published') pub++;
      if (f.status === 'quarantined') quar++;
    });
  });

  let html = `
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;
                gap:12px;margin-bottom:16px;">
      <div>
        <h1 style="margin:0;font-size:24px;">${esc(REPORT.title)}</h1>
        <div class="mono" style="font-size:12px;color:var(--muted);margin-top:4px;">
          Generated ${esc(REPORT.generated_at)}
        </div>
      </div>
      <span class="badge badge-info">Offline Self-Contained Report</span>
    </div>
    <div class="kpi-grid">
      <div class="kpi"><div class="kpi-label">Jobs</div><div class="kpi-val">${jobs.length}</div></div>
      <div class="kpi"><div class="kpi-label">Published EXRs</div><div class="kpi-val" style="color:var(--pass)">${pub}</div></div>
      <div class="kpi"><div class="kpi-label">Quarantined</div><div class="kpi-val" style="color:var(--warn)">${quar}</div></div>
      <div class="kpi"><div class="kpi-label">Pre-Flight Blocked</div><div class="kpi-val" style="color:var(--fail)">${preBlocked}</div></div>
    </div>
  `;

  let wipeIdx = 0;
  jobs.forEach(j => {
    const first = (j.frames || [])[0];
    const framesHtml = (j.frames || []).slice(0, 1).map(f => {
      const idx = wipeIdx++;
      const leftSrc = f.input_preview || f.output_preview || '';
      const rightSrc = f.output_preview || f.input_preview || '';
      const iouText = f.edge_iou != null ? 'IoU ' + (f.edge_iou * 100).toFixed(1) + '%' : 'N/A';
      return `
        <div class="grid-2" style="margin-top:12px;">
          <div>
            <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
              <strong>Frame ${f.frame}: Input vs Output</strong>
              <span class="mono" style="color:var(--muted);">${esc(f.resolution)}</span>
            </div>
            <div class="wipe-wrap">
              <img src="${esc(rightSrc)}" alt="Output" />
              <img src="${esc(leftSrc)}" id="wtop-${idx}" class="wipe-top" alt="Input" />
              <div class="wipe-divider" id="wdiv-${idx}"></div>
              <span class="wipe-label left">Input</span>
              <span class="wipe-label right">${f.output_preview ? 'Output' : 'Blocked'}</span>
            </div>
            <input class="wipe-slider" type="range" min="0" max="100" value="50"
                   oninput="moveWipe(${idx}, this.value)" />
          </div>
          <div>
            <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px;">
              <strong>Sobel Edge-Drift Diagnostic</strong>
              <span class="mono">${esc(iouText)}</span>
            </div>
            <div class="wipe-wrap">
              <img src="${esc(f.edge_overlay || leftSrc)}" alt="Diagnostic" />
            </div>
            <div class="legend" style="margin-top:8px;">
              <span><i class="dot" style="background:#22c55e;"></i>Matched</span>
              <span><i class="dot" style="background:#38bdf8;"></i>Missing CG</span>
              <span><i class="dot" style="background:#ef4444;"></i>Drifted AI</span>
            </div>
          </div>
        </div>
      `;
    }).join('');

    const attrPairs = first ? Object.entries(first.exr_attributes || {}) : [];
    const attrsHtml = attrPairs.length
      ? '<div style="margin-top:12px;"><strong style="font-size:13px;">Stamped EXR Attributes:</strong><pre class="codebox" style="margin-top:6px;">' +
        esc(attrPairs.map(([k, v]) => k + ' = ' + v).join('\\n')) + '</pre></div>'
      : '';

    const gateList = [j.preflight_report, j.postflight_report].filter(Boolean);
    const checksHtml = gateList.map(rep => `
      <div style="margin-top:12px;">
        <div style="display:flex;justify-content:space-between;margin-bottom:4px;font-size:13px;">
          <strong>${esc(rep.stage.toUpperCase())} GATE</strong>
          ${badge(rep.passed ? 'pass' : 'fail')}
        </div>
        <table class="checks">
          <thead><tr><th>Status</th><th>Check</th><th>Details</th></tr></thead>
          <tbody>
            ${(rep.checks || []).map(c => `<tr><td>${badge(c.status)}</td><td class="mono">${esc(c.check_name)}</td><td>${esc(c.message)}</td></tr>`).join('')}
          </tbody>
        </table>
      </div>
    `).join('');

    html += `
      <section class="card scenario-card">
        <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:center;">
          <div>
            <span class="badge badge-info">${esc(j.scenario_tag)}</span>
            <strong style="font-size:17px;margin-left:8px;">${esc(j.title)}</strong>
          </div>
          ${badge(j.status)}
        </div>
        <div class="mono" style="font-size:12px;color:var(--muted);margin-top:6px;">
          ${esc(j.shot_label)} | ${esc(j.pass_label)} | ${esc(j.colorspace)} |
          GPU Dispatched: ${j.gpu_prompts_dispatched}
        </div>
        ${framesHtml}
        ${attrsHtml}
        ${checksHtml}
      </section>
    `;
  });

  if (REPORT.graph_diff) {
    html += `
      <section class="card scenario-card">
        <h2 style="margin:0 0 8px 0;font-size:17px;">Workflow Graph Diff (${esc(REPORT.graph_diff.v1_label)} &rarr; ${esc(REPORT.graph_diff.v2_label)})</h2>
        <pre class="codebox">${esc(REPORT.graph_diff.human_text)}</pre>
      </section>
    `;
  }

  document.getElementById('root').innerHTML = html;
})();
</script>
</body>
</html>
""".replace("__SHARED_CSS__", _SHARED_CSS)
