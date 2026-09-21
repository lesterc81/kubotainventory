// ─── CSRF helper for JSON/AJAX POSTs ─────────────────────────────────
window.csrfToken = () => {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.content : '';
};
function csrfHeaders(extra) {
  return Object.assign({ 'X-CSRFToken': window.csrfToken() }, extra || {});
}

// Download a file behind auth. In the desktop exe, export endpoints write the
// file to the app's Exports folder and return a "Saved" page that auto-opens it
// (WebView2 drops attachment downloads). In a normal browser we first verify the
// endpoint (surfacing 500/403), then open the URL so the browser downloads it.
async function safeDownload(url) {
  if (window.pywebview && pywebview.api) {
    window.location.href = url;
    return;
  }
  try {
    const r = await fetch(url, { credentials: 'same-origin' });
    if (!r.ok) {
      const body = await r.text().catch(() => '');
      alert('Download failed (HTTP ' + r.status + '): ' + body.slice(0, 200));
      return;
    }
  } catch (err) {
    alert('Download failed: ' + err.message);
    return;
  }
  window.location.href = url;
}

// Async export: POST starts a background generation job, then poll the status
// endpoint and navigate to the download URL once it is ready. Works in both the
// desktop exe (Saved page + OS open) and a normal browser (attachment download).
async function asyncDownload(url) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'same-origin',
      headers: csrfHeaders({ 'Content-Type': 'application/json' })
    });
    const body = await r.json().catch(() => null);
    if (!r.ok || !body || !body.job_id) {
      alert('Export could not be started (HTTP ' + r.status + ').');
      return;
    }
    const jobId = body.job_id;
    for (let i = 0; i < 120; i++) {
      await new Promise(res => setTimeout(res, 500));
      const s = await fetch(url + '/status/' + jobId, { credentials: 'same-origin' });
      const st = await s.json().catch(() => null);
      if (!st || !st.status) continue;
      if (st.status === 'done') {
        window.location.href = url + '/download/' + jobId;
        return;
      }
      if (st.status === 'error') {
        alert('Export failed: ' + (st.error || 'unknown error'));
        return;
      }
    }
    alert('Export timed out after 60 seconds.');
  } catch (err) {
    alert('Export failed: ' + err.message);
  }
}

// ─── Double-submit guard: disable submit buttons while a form is submitting
document.addEventListener('submit', function (e) {
  const form = e.target;
  if (!(form instanceof HTMLFormElement)) return;
  if (form.dataset.noGuard === '1') return;
  const hidden = document.createElement('input');
  hidden.type = 'hidden';
  hidden.name = '_submitted';
  hidden.value = '1';
  form.appendChild(hidden);
  const buttons = form.querySelectorAll('button[type="submit"], button:not([type])');
  buttons.forEach(function (b) { b.disabled = true; });
});

// ─── Theme change signal (charts + fx restyle from CSS tokens) ──────────
const html = document.documentElement;
const darkIcon = document.getElementById('darkIcon');
function applyTheme(dark) {
  html.dataset.theme = dark ? 'dark' : 'light';
  if (darkIcon) darkIcon.textContent = dark ? 'light_mode' : 'dark_mode';
  localStorage.setItem('theme', dark ? 'dark' : 'light');
  document.dispatchEvent(new CustomEvent('themechange'));
}
(function () {
  const saved = localStorage.getItem('theme');
  applyTheme(saved === 'dark' || (!saved && window.matchMedia('(prefers-color-scheme: dark)').matches));
})();

const darkToggle = document.getElementById('darkToggle');
if (darkToggle) {
  darkToggle.addEventListener('click', () => applyTheme(html.dataset.theme !== 'dark'));
}

// ─── Sidebar Toggle (mobile) ────────────────────────────────────────────
const sidebar = document.getElementById('sidebar');
const hamburger = document.getElementById('hamburgerToggle');
const sidebarOverlay = document.getElementById('sidebarOverlay');

function openSidebar() {
  sidebar?.classList.add('open');
  sidebarOverlay?.classList.add('show');
  document.body.style.overflow = 'hidden';
}
function closeSidebar() {
  sidebar?.classList.remove('open');
  sidebarOverlay?.classList.remove('show');
  document.body.style.overflow = '';
}

if (hamburger) {
  hamburger.addEventListener('click', () => {
    sidebar?.classList.contains('open') ? closeSidebar() : openSidebar();
  });
}
if (sidebarOverlay) {
  sidebarOverlay.addEventListener('click', closeSidebar);
}

// ─── Sidebar collapse (desktop icon rail) ────────────────────────────────
const collapseBtn = document.getElementById('sidebarCollapse');
const collapseIcon = collapseBtn?.querySelector('.material-icons-round');
const RAIL_KEY = 'sidebar-rail';
function setCollapsed(collapsed) {
  document.body.classList.toggle('sidebar-collapsed', collapsed);
  if (collapseIcon) collapseIcon.textContent = collapsed ? 'chevron_right' : 'chevron_left';
  collapseBtn?.setAttribute('aria-label', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  collapseBtn?.setAttribute('title', collapsed ? 'Expand sidebar' : 'Collapse sidebar');
  if (window.innerWidth > 768) localStorage.setItem(RAIL_KEY, collapsed ? '1' : '0');
}
if (collapseBtn) {
  collapseBtn.addEventListener('click', () => {
    setCollapsed(!document.body.classList.contains('sidebar-collapsed'));
  });
}
if (window.innerWidth > 768 && localStorage.getItem(RAIL_KEY) === '1') setCollapsed(true);

// ─── Tabs ─────────────────────────────────────────────────────────────────
document.querySelectorAll('.tab-item[data-tab]').forEach(btn => {
  btn.addEventListener('click', () => {
    const target = btn.dataset.tab;
    document.querySelectorAll('.tab-item').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById(target)?.classList.add('active');
  });
});

// ─── Dialogs ──────────────────────────────────────────────────────────────
function openDialog(id) {
  document.getElementById(id)?.classList.add('show');
}
function closeDialog(id) {
  document.getElementById(id)?.classList.remove('show');
}
document.querySelectorAll('[data-dialog-open]').forEach(btn => {
  btn.addEventListener('click', () => openDialog(btn.dataset.dialogOpen));
});
document.querySelectorAll('[data-dialog-close]').forEach(btn => {
  btn.addEventListener('click', () => closeDialog(btn.dataset.dialogClose));
});
document.querySelectorAll('.dialog-overlay').forEach(overlay => {
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) overlay.classList.remove('show');
  });
});

// ─── Archive / Confirm ────────────────────────────────────────────────────
document.querySelectorAll('[data-confirm]').forEach(btn => {
  btn.addEventListener('click', (e) => {
    if (!confirm(btn.dataset.confirm || 'Are you sure?')) e.preventDefault();
  });
});

// ─── Auto-dismiss snackbars (anime.js handles exit animation now) ──────────
document.querySelectorAll('.snackbar:not([data-anime-done])').forEach(el => {
  setTimeout(() => el.remove(), 6000);
});

// ─── Multi-select asset checkboxes (accountability form) ──────────────────
const assetIdsField = document.getElementById('assetIdsField');
if (assetIdsField) {
  function updateAssetIds() {
    const checked = Array.from(document.querySelectorAll('.asset-check:checked')).map(c => c.value);
    assetIdsField.value = JSON.stringify(checked);
  }
  document.querySelectorAll('.asset-check').forEach(cb => cb.addEventListener('change', updateAssetIds));
}

// ─── Employee autocomplete ────────────────────────────────────────────────
const empInput = document.getElementById('employeeSearch');
const empIdField = document.getElementById('employee_id');
const empResults = document.getElementById('empResults');
if (empInput && empIdField) {
  let debounce;
  empInput.addEventListener('input', () => {
    clearTimeout(debounce);
    debounce = setTimeout(async () => {
      const q = empInput.value.trim();
      if (!q) { empResults.innerHTML = ''; return; }
      const res = await fetch(`/api/employees/search?q=${encodeURIComponent(q)}`);
      const body = await res.json();
      const data = body && Array.isArray(body.data) ? body.data : [];
      empResults.innerHTML = data.map(e =>
        `<div class="autocomplete-item" data-id="${e.id}" data-text="${e.text}">${e.text}</div>`
      ).join('');
    }, 250);
  });
  empResults?.addEventListener('click', e => {
    const item = e.target.closest('.autocomplete-item');
    if (item) {
      empInput.value = item.dataset.text;
      empIdField.value = item.dataset.id;
      empResults.innerHTML = '';
    }
  });
}

// ─── Chart helpers (used in dashboard) ───────────────────────────────────
// Palette is read from the CSS tokens every render, so charts restyle live
// when the theme toggles without page reload.
function cssVar(name, fallback) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}
function chartPalette() {
  return {
    accent: cssVar('--chart-1', '#22d3c5'),
    ticks: cssVar('--ink-muted', '#96a3bd'),
    grid: cssVar('--chart-grid', 'rgba(150,163,189,0.14)'),
    series: [1, 2, 3, 4, 5].map(i => cssVar('--chart-' + i, '#22d3c5'))
      .concat([1, 2, 3].map(i => cssVar('--chart-n' + i, '#56637c'))),
    font: cssVar('--font-body', 'DM Sans, sans-serif')
  };
}

const chartDefs = {};
function drawChart(canvasId, def) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (ctx._chartInstance) ctx._chartInstance.destroy();
  const p = chartPalette();
  const baseOpts = {
    responsive: true,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { color: p.grid }, ticks: { color: p.ticks, font: { family: p.font } } },
      y: { beginAtZero: true, grid: { color: p.grid }, ticks: { color: p.ticks, font: { family: p.font } } }
    }
  };
  const opts = Object.assign({}, baseOpts);
  switch (def.type) {
    case 'bar':
      opts.plugins.legend.display = def.legend !== false;
      ctx._chartInstance = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: def.labels,
          datasets: [{
            data: def.values,
            backgroundColor: def.color ? hexToRgba(def.color, 0.85) : def.values.map((_, i) => hexToRgba(p.series[i % p.series.length], 0.85)),
            borderColor: def.color || p.accent,
            borderWidth: 1.5,
            borderRadius: 6
          }]
        },
        options: opts
      });
      break;
    case 'doughnut':
      ctx._chartInstance = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: def.labels,
          datasets: [{
            data: def.values,
            backgroundColor: def.color || def.labels.map((_, i) => p.series[i % p.series.length]),
            borderColor: 'transparent',
            borderWidth: 2,
            hoverOffset: 6
          }]
        },
        options: {
          responsive: true,
          plugins: {
            legend: { position: 'bottom', labels: { color: p.ticks, font: { family: p.font, size: 12 }, usePointStyle: true, pointStyle: 'circle' } },
            tooltip: { backgroundColor: cssVar('--glass-bg-pop', '#161e34') }
          },
          cutout: '65%'
        }
      });
      break;
  }
}
function hexToRgba(hex, alpha) {
  const m = hex.replace('#', '');
  if (m.length === 3) return hexToRgba(m.split('').map(c => c + c).join(''), alpha);
  const n = parseInt(m, 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

window.renderBarChart = function (canvasId, labels, values, color) {
  chartDefs[canvasId] = { type: 'bar', labels, values, color };
  drawChart(canvasId, chartDefs[canvasId]);
};
window.renderDoughnut = function (canvasId, labels, values, colors) {
  chartDefs[canvasId] = { type: 'doughnut', labels, values, color: colors };
  drawChart(canvasId, chartDefs[canvasId]);
};

document.addEventListener('themechange', () => {
  Object.keys(chartDefs).forEach(id => drawChart(id, chartDefs[id]));
});
