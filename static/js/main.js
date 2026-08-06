// ─── CSRF helper for JSON/AJAX POSTs ─────────────────────────────────
window.csrfToken = () => {
  const m = document.querySelector('meta[name="csrf-token"]');
  return m ? m.content : '';
};
function csrfHeaders(extra) {
  return Object.assign({ 'X-CSRFToken': window.csrfToken() }, extra || {});
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

// ─── Dark Mode ──────────────────────────────────────────────────────────
const html = document.documentElement;
const darkIcon = document.getElementById('darkIcon');
function applyTheme(dark) {
  html.dataset.theme = dark ? 'dark' : 'light';
  if (darkIcon) darkIcon.textContent = dark ? 'light_mode' : 'dark_mode';
  localStorage.setItem('theme', dark ? 'dark' : 'light');
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
window.renderBarChart = function(canvasId, labels, values, color = '#C73E3E') {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (ctx._chartInstance) ctx._chartInstance.destroy();
  ctx._chartInstance = new Chart(ctx, {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: color + 'CC', borderColor: color, borderWidth: 1.5, borderRadius: 6 }]
    },
    options: {
      responsive: true,
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, ticks: { precision: 0 } } }
    }
  });
};
window.renderDoughnut = function(canvasId, labels, values, colors) {
  const ctx = document.getElementById(canvasId);
  if (!ctx) return;
  if (ctx._chartInstance) ctx._chartInstance.destroy();
  ctx._chartInstance = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: colors, borderWidth: 2, hoverOffset: 6 }]
    },
    options: {
      responsive: true,
      plugins: { legend: { position: 'bottom', labels: { font: { size: 12 } } } },
      cutout: '65%'
    }
  });
};
