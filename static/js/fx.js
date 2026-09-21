/* Advanced glassmorphism background (Three.js).
 *
 * Adds a fixed, full-screen animated canvas behind the app:
 *   - "full" mode (login/auth pages via <body data-fx="full">): floating glass
 *     orbs + soft particle field + fog.
 *   - "ambient" mode (default, data pages): dimmer, fewer particles, no orbs,
 *     so tables/cards stay readable on office hardware.
 *
 * Mirrors the theme (light/dark + --hue), pauses when the tab is hidden,
 * respects prefers-reduced-motion, and self-disables if WebGL is unavailable.
 */
(function () {
  if (window.FX_SCENE) return;
  window.FX_SCENE = true;

  var canvas = document.getElementById('fx-canvas');
  if (!canvas || !window.THREE) { setBody(false); return; }

  var reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // Low-end device gate: trim the scene before it renders.
  var lowEnd = (navigator.hardwareConcurrency && navigator.hardwareConcurrency <= 4) ||
               (navigator.deviceMemory && navigator.deviceMemory <= 4);

  // 1) WebGL probe
  var renderer;
  try {
    renderer = new THREE.WebGLRenderer({
      canvas: canvas, alpha: true, antialias: !reduced && !lowEnd, powerPreference: 'low-power'
    });
  } catch (e) { setBody(false); return; }

  var scene = new THREE.Scene();
  var camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);
  camera.position.z = 16;

  var full = document.body.getAttribute('data-fx') === 'full';

  // 2) Palette from CSS custom properties
  function hue() {
    var h = getComputedStyle(document.documentElement).getPropertyValue('--hue');
    return h ? parseFloat(h) || 215 : 215;
  }
  function isDark() { return document.documentElement.getAttribute('data-theme') === 'dark'; }

  function hsl(h, s, l) {
    return new THREE.Color('hsl(' + h + ',' + s + '%,' + l + '%)');
  }
  var P3 = function () {
    var h = hue(), d = isDark();
    var base = d ? 8 : 26;
    return {
      bg:    hsl(h, d ? 18 : 24, d ? 12 : 86),
      pLow:  hsl(h, 70, d ? 55 : 72),
      pHigh: hsl((h + 150) % 360, 65, d ? 62 : 88),
      glass: hsl(h, 60, d ? 62 : 74),
      wire:  hsl(h, 45, d ? 80 : 92)
    };
  };

  var colors = P3();
  scene.fog = new THREE.Fog(colors.bg, full ? 14 : 22, full ? 40 : 55);

  // 3) Glass orbs (full mode only)
  var glass = [], wireChildren = [];
  function makeOrb(seed) {
    var g = new THREE.IcosahedronGeometry(1 + Math.random() * 0.7, 1);
    var m = new THREE.MeshPhysicalMaterial({
      color: colors.glass,
      transparent: true,
      opacity: full ? 0.22 : 0.12,
      roughness: 0.25,
      metalness: 0.05,
      clearcoat: 0.9,
      clearcoatRoughness: 0.25,
      depthWrite: false
    });
    var mesh = new THREE.Mesh(g, m);
    mesh.position.set(
      (Math.random() - 0.5) * (full ? 26 : 14),
      (Math.random() - 0.5) * (full ? 15 : 7),
      -6 - Math.random() * 8
    );
    var s = 0.6 + Math.random() * (full ? 1.6 : 0.7);
    mesh.scale.setScalar(s);
    mesh.userData = {
      phase: Math.random() * Math.PI * 2,
      amp: 0.4 + Math.random() * 0.7,
      spin: (Math.random() - 0.5) * 0.06
    };
    scene.add(mesh);
    glass.push(mesh);

    if (full && !lowEnd && Math.random() < 0.45) {
      var wire = new THREE.Mesh(g, new THREE.MeshBasicMaterial({
        color: colors.wire, wireframe: true, transparent: true, opacity: 0.12
      }));
      wire.scale.copy(mesh.scale);
      wire.position.copy(mesh.position);
      scene.add(wire);
      wireChildren.push(wire);
    }
  }

  var ORB_COUNT = full ? (lowEnd ? 5 : 11) : (lowEnd ? 0 : 4);
  for (var i = 0; i < ORB_COUNT; i++) makeOrb(i);

  // 4) Particle field — scaled down on data pages and low-end hardware
  function makeField(n, spread, size) {
    var positions = new Float32Array(n * 3);
    for (var j = 0; j < n; j++) {
      positions[j * 3]     = (Math.random() - 0.5) * spread;
      positions[j * 3 + 1] = (Math.random() - 0.5) * spread * 0.6;
      positions[j * 3 + 2] = -4 - Math.random() * 20;
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    var mat = new THREE.PointsMaterial({
      color: colors.pHigh, size: size, transparent: true,
      opacity: full ? 0.42 : 0.3, sizeAttenuation: true, depthWrite: false
    });
    var pts = new THREE.Points(geo, mat);
    scene.add(pts);
    return pts;
  }
  var field = makeField(
    full ? (lowEnd ? 150 : 340) : (lowEnd ? 50 : 110),
    full ? 42 : 30,
    full ? 0.14 : 0.1
  );

  // 5) Lights
  scene.add(new THREE.HemisphereLight(colors.pHigh, colors.bg, 0.85));
  var key = new THREE.DirectionalLight(colors.pHigh, 0.9); key.position.set(6, 8, 8);
  scene.add(key);
  var rim = new THREE.DirectionalLight(colors.wire, 0.5); rim.position.set(-6, -4, -6);
  scene.add(rim);

  // 6) Resize + pointer parallax
  function resize() {
    var w = canvas.clientWidth || window.innerWidth;
    var hh = canvas.clientHeight || window.innerHeight;
    renderer.setSize(w, hh, false);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, full ? 2 : 1.5));
    camera.aspect = w / hh;
    camera.updateProjectionMatrix();
  }
  window.addEventListener('resize', resize);
  resize();

  var mx = 0, my = 0;
  if (!reduced && !window.matchMedia('(pointer: coarse)').matches) {
    document.addEventListener('mousemove', function (e) {
      mx = (e.clientX / window.innerWidth) * 2 - 1;
      my = (e.clientY / window.innerHeight) * 2 - 1;
    });
  }

  // 7) Theme bridge: rebuild palette when light/dark or --hue changes
  var lastHue = hue(), lastDark = isDark();
  new MutationObserver(function () {
    var h = hue(), d = isDark();
    if (h === lastHue && d === lastDark) return;
    lastHue = h; lastDark = d;
    colors = P3();
    scene.fog.color.copy(colors.bg);
    for (var i = 0; i < glass.length; i++) glass[i].material.color.copy(colors.glass);
    for (var w = 0; w < wireChildren.length; w++) wireChildren[w].material.color.copy(colors.wire);
    field.material.color.copy(colors.pHigh);
    key.color.copy(colors.pHigh); rim.color.copy(colors.wire);
    scene.children.forEach(function (c) { if (c.isHemisphereLight) c.color.copy(colors.pHigh); });
  }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style'] });

  // 8) Render loop
  var clock = new THREE.Clock();
  var running = true;
  function tick() {
    requestAnimationFrame(tick);
    if (!running || reduced) return;
    var t = clock.getElapsedTime();
    for (var i = 0; i < glass.length; i++) {
      var m = glass[i];
      m.rotation.x += m.userData.spin * 0.3;
      m.rotation.y += m.userData.spin;
      m.position.y += Math.sin(t * 0.4 + m.userData.phase) * 0.004;
      if (wireChildren[i]) { wireChildren[i].rotation.copy(m.rotation); wireChildren[i].position.y = m.position.y; }
    }
    field.rotation.y = t * 0.012;
    // Subtle scroll parallax so the depth field reacts to the page
    var drift = ((window.scrollY || 0) / Math.max(1, document.documentElement.scrollHeight - window.innerHeight)) * 1.8 - 0.9;
    camera.position.x += (mx * 1.6 - camera.position.x) * 0.03;
    camera.position.y += (drift - my * 1.1 - camera.position.y) * 0.03;
    camera.lookAt(0, 0, -4);
    renderer.render(scene, camera);
  }
  tick();

  document.addEventListener('visibilitychange', function () {
    running = !document.hidden;
    clock.getDelta(); // avoid jump
    if (running) tick();
  });

  // 9) Fallback: body only gets .has-fx once the scene actually renders
  var rendered = false;
  var onFirst = function () {
    if (rendered) return;
    rendered = true;
    document.body.classList.add('has-fx');
    renderer.render(scene, camera);
  };

  function setBody(ok) {
    document.body.classList.toggle('has-fx', ok);
  }
  // Let the first frame prove WebGL actually works before showing the canvas.
  requestAnimationFrame(function () {
    try { renderer.render(scene, camera); onFirst(); }
    catch (e) { setBody(false); }
  });
})();