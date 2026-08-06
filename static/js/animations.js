// ─── prefers-reduced-motion guard ────────────────────────────────────
function _anim(params) {
  if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    if (params.complete) params.complete();
    return null;
  }
  return anime(params);
}

// ─── 1. Page Stamp Entrance ──────────────────────────────────────────
(function stampPageIn() {
  if (typeof anime === 'undefined') return;
  var main = document.querySelector('.main-content, .auth-main');
  if (!main) return;
  _anim({
    targets: main,
    scale: [0.98, 1],
    opacity: [0, 1],
    duration: 500,
    easing: 'easeOutElastic(1, .5)',
    begin: function () { main.style.opacity = '1'; }
  });
})();

// ─── 2. DOMContentLoaded choreography ────────────────────────────────
document.addEventListener('DOMContentLoaded', function () {
  if (typeof anime === 'undefined') return;

  // ── Cards stagger ─────────────────────────────────────────────────
  var cards = document.querySelectorAll('.card');
  if (cards.length) {
    cards.forEach(function (c) { c.style.opacity = '0'; });
    _anim({
      targets: cards,
      translateY: [16, 0],
      opacity: [0, 1],
      duration: 400,
      delay: _anim.stagger ? anime.stagger(70, { start: 200 }) : 0,
      easing: 'easeOutCubic',
      complete: function () {
        cards.forEach(function (c) { c.style.opacity = ''; c.style.transform = ''; });
      }
    });
  }

  // ── Stat-card stagger ──────────────────────────────────────────────
  var statCards = document.querySelectorAll('.stat-card');
  if (statCards.length) {
    statCards.forEach(function (s) { s.style.opacity = '0'; });
    _anim({
      targets: statCards,
      translateY: [20, 0],
      opacity: [0, 1],
      duration: 450,
      delay: anime.stagger(60, { start: 150 }),
      easing: 'easeOutCubic',
      complete: function () {
        statCards.forEach(function (s) { s.style.opacity = ''; s.style.transform = ''; });
      }
    });
  }

  // ── Page header ────────────────────────────────────────────────────
  var headers = document.querySelectorAll('.page-header');
  if (headers.length) {
    headers.forEach(function (h) { h.style.opacity = '0'; });
    _anim({
      targets: headers,
      translateY: [12, 0],
      opacity: [0, 1],
      duration: 350,
      delay: 100,
      easing: 'easeOutCubic',
      complete: function () { headers.forEach(function (h) { h.style.opacity = ''; }); }
    });
  }

  // ── Breadcrumb stagger ─────────────────────────────────────────────
  var crumbs = document.querySelectorAll('.breadcrumb > *');
  if (crumbs.length) {
    crumbs.forEach(function (c) { c.style.opacity = '0'; });
    _anim({
      targets: crumbs,
      translateX: [-8, 0],
      opacity: [0, 1],
      duration: 300,
      delay: anime.stagger(60, { start: 150 }),
      easing: 'easeOutQuad',
      complete: function () { crumbs.forEach(function (c) { c.style.opacity = ''; c.style.transform = ''; }); }
    });
  }

  // ── Table row stagger ──────────────────────────────────────────────
  document.querySelectorAll('.table-wrapper table tbody').forEach(function (tbody) {
    var rows = tbody.querySelectorAll('tr');
    if (rows.length < 2) return;
    rows.forEach(function (r) { r.style.opacity = '0'; });
    _anim({
      targets: rows,
      translateY: [6, 0],
      opacity: [0, 1],
      duration: 300,
      delay: anime.stagger(35),
      easing: 'easeOutQuad',
      complete: function () {
        rows.forEach(function (r) { r.style.opacity = ''; r.style.transform = ''; });
      }
    });
  });

  // ── Chip stamp landing ─────────────────────────────────────────────
  var chips = document.querySelectorAll('.chip');
  if (chips.length) {
    chips.forEach(function (c) {
      c.style.opacity = '0';
      c.style.transform = 'scale(0.7) translateY(-6px) rotate(' + (Math.random() * 4 - 2) + 'deg)';
    });
    _anim({
      targets: chips,
      scale: [0.7, 1],
      translateY: [-6, 0],
      opacity: [0, 1],
      rotate: [function (el) { return el.style.transform.match(/rotate\(([^)]+)\)/)[1]; }, '-0.5deg'],
      duration: 450,
      delay: anime.stagger(50, { start: 400 }),
      easing: 'easeOutElastic(1, .4)',
      complete: function () {
        chips.forEach(function (c) { c.style.opacity = ''; c.style.transform = ''; });
      }
    });
    // ── Chip hover wiggle (randomized) ─────────────────────────────
    chips.forEach(function (chip) {
      chip.addEventListener('mouseenter', function () {
        _anim({
          targets: chip,
          rotate: [null, (Math.random() * 3 - 1.5) + 'deg'],
          scale: [null, 0.97],
          duration: 200,
          easing: 'easeOutQuad'
        });
      });
      chip.addEventListener('mouseleave', function () {
        _anim({
          targets: chip,
          rotate: [null, '-0.5deg'],
          scale: [null, 1],
          duration: 200,
          easing: 'easeOutCubic'
        });
      });
    });
  }

  // ── Snackbar elastic drop-in ───────────────────────────────────────
  document.querySelectorAll('.snackbar').forEach(function (snack) {
    // If this snackbar was already handled (e.g. by the global flash loop skip), skip
    if (snack.dataset.animeDone) return;
    snack.dataset.animeDone = '1';
    _anim({
      targets: snack,
      translateY: [-20, 0],
      opacity: [0, 1],
      duration: 500,
      easing: 'easeOutElastic(1, .5)'
    });
    // Auto-dismiss with anime exit (overrides main.js setTimeout remove)
    setTimeout(function () {
      _anim({
        targets: snack,
        translateY: [null, -12],
        opacity: [1, 0],
        height: [snack.offsetHeight + 'px', '0px'],
        marginBottom: [null, '0px'],
        padding: [null, '0px 18px'],
        duration: 350,
        easing: 'easeInCubic',
        complete: function () { snack.remove(); }
      });
    }, 5500);
  });

  // ── Dialog entrance/exit enhancement ──────────────────────────────
  document.querySelectorAll('.dialog-overlay').forEach(function (overlay) {
    var dialog = overlay.querySelector('.dialog');
    if (!dialog) return;

    var _origAdd = overlay.classList.add.bind(overlay.classList);
    overlay.classList.add = function (cls) {
      _origAdd(cls);
      if (cls === 'show') {
        // Overlay fade
        _anim({ targets: overlay, opacity: [0, 1], duration: 200, easing: 'easeOutQuad' });
        // Dialog scale + stagger children
        var children = dialog.querySelectorAll('.dialog-header, .dialog-body, .dialog-footer');
        dialog.style.transform = 'scale(0.92)';
        dialog.style.opacity = '0';
        _anim({
          targets: dialog,
          scale: [0.92, 1],
          opacity: [0, 1],
          duration: 350,
          easing: 'easeOutElastic(1, .5)',
          complete: function () { dialog.style.transform = ''; dialog.style.opacity = ''; }
        });
        if (children.length) {
          children.forEach(function (ch) { ch.style.opacity = '0'; });
          _anim({
            targets: children,
            translateY: [10, 0],
            opacity: [0, 1],
            duration: 250,
            delay: anime.stagger(60),
            easing: 'easeOutCubic',
            complete: function () {
              children.forEach(function (ch) { ch.style.opacity = ''; ch.style.transform = ''; });
            }
          });
        }
      }
    };

    var _origRemove = overlay.classList.remove.bind(overlay.classList);
    overlay.classList.remove = function (cls) {
      if (cls === 'show') {
        _anim({
          targets: overlay,
          opacity: [1, 0],
          duration: 180,
          easing: 'easeInQuad'
        });
        _anim({
          targets: dialog,
          scale: [1, 0.95],
          opacity: [1, 0],
          duration: 180,
          easing: 'easeInQuad',
          complete: function () { _origRemove(cls); }
        });
        return;
      }
      _origRemove(cls);
    };

    // Also patch close button clicks inside the dialog
    dialog.querySelectorAll('[data-dialog-close]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        overlay.classList.remove('show');
      });
    });
  });

  // ── Button stamp press ─────────────────────────────────────────────
  document.querySelectorAll('.btn-primary, .btn-danger').forEach(function (btn) {
    btn.addEventListener('click', function () {
      _anim({
        targets: btn,
        scale: [1, 0.93, 1],
        duration: 350,
        easing: 'easeOutElastic(1, .5)'
      });
    });
  });

  // ── Sidebar active indicator ──────────────────────────────────────
  (function sidebarIndicator() {
    var activeLink = document.querySelector('.sidebar-link.active');
    var sidebarNav = document.querySelector('.sidebar-nav');
    if (!activeLink || !sidebarNav) return;

    var indicator = sidebarNav.querySelector('.sidebar-active-indicator');
    if (!indicator) {
      indicator = document.createElement('div');
      indicator.className = 'sidebar-active-indicator';
      sidebarNav.appendChild(indicator);
    }
    // Position at active link with anime entrance
    var linkRect = activeLink.getBoundingClientRect();
    var navRect = sidebarNav.getBoundingClientRect();
    indicator.style.cssText =
      'position:absolute;left:0;width:3px;background:var(--stamp-red);border-radius:0 2px 2px 0;' +
      'top:' + (linkRect.top - navRect.top + 4) + 'px;' +
      'height:' + (linkRect.height - 8) + 'px;' +
      'transform:scaleY(0);transform-origin:top center';
    _anim({
      targets: indicator,
      scaleY: [0, 1],
      duration: 350,
      easing: 'easeOutElastic(1, .4)'
    });
  })();

  // ── Empty state icon breathe ───────────────────────────────────────
  document.querySelectorAll('.empty-state .material-icons-round').forEach(function (icon) {
    _anim({
      targets: icon,
      scale: [1, 1.06],
      duration: 3000,
      direction: 'alternate',
      easing: 'easeInOutSine',
      loop: true
    });
  });
});
