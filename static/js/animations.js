// ─── Motion layer (CSS-first, no animation library) ───────────────────
// Entry animations are defined in CSS. This file only assigns stagger
// delays and handles exit states. All animation is disabled under
// prefers-reduced-motion (handled in CSS).

(function () {
  var REDUCED = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (REDUCED) return;

  // ─── Staggered card/stat entrance ─────────────────────────────────
  var groups = document.querySelectorAll('.stats-grid, .charts-grid, .card-stack');
  groups.forEach(function (group) {
    var items = group.children;
    Array.prototype.forEach.call(items, function (el, i) {
      el.style.setProperty('--d', (i * 40) + 'ms');
      el.style.animation = 'fadeUp 0.42s cubic-bezier(0.2,0.8,0.3,1) both';
      el.style.animationDelay = 'calc(var(--d) + 60ms)';
    });
  });

  // ─── Snackbar exit (complement the CSS drop-in) ───────────────────
  document.querySelectorAll('.snackbar').forEach(function (snack) {
    if (snack.dataset.animeDone) return;
    snack.dataset.animeDone = '1';
    setTimeout(function () {
      if (!snack.isConnected) return;
      snack.classList.add('snackbar-leaving');
      setTimeout(function () { snack.remove(); }, 240);
    }, 5500);
  });

  // cleanup inline animation after it plays (avoids stale delays on re-render)
  window.addEventListener('load', function () {
    setTimeout(function () {
      document.querySelectorAll('.stats-grid *, .charts-grid *, .card-stack *').forEach(function (el) {
        el.style.animation = ''; el.style.animationDelay = '';
      });
    }, 1800);
  });
})();