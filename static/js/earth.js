/* 3D 地球模块：星空/地球/大气辉光/经纬网格 + 动态线索点（按 stage 配色）+ 飞线。
   依赖 static/vendor/three.min.js，暴露 window.EARTH.setDots(dots)。 */
(function () {
  var $ = function (i) { return document.getElementById(i); };
  window.__err = window.__err || [];
  var el = $("g3d"), dgl = $("dgl");
  var webgl = false, globe, cam, rn, sm, hot = [], lines = [];
  var STAGE_COLOR = { S1: 0x4d9de0, S2: 0x8a63f5, S3: 0xf5a623, S4: 0x2ecc8f };
  try {
    if (!document.createElement("canvas").getContext("webgl") &&
        !document.createElement("canvas").getContext("experimental-webgl")) throw new Error("nowebgl");
    var D2R = Math.PI / 180;
    function latLon(lat, lon, r) {
      var a = lat * D2R, b = lon * D2R;
      return new THREE.Vector3(Math.cos(a) * Math.sin(b) * r, Math.sin(a) * r, Math.cos(a) * Math.cos(b) * r);
    }
    var W = el.clientWidth, H = el.clientHeight;
    var sc = new THREE.Scene(); sc.background = new THREE.Color(0x060a12);
    cam = new THREE.PerspectiveCamera(45, W / H, 0.1, 300); cam.position.set(0, 0, 4);
    rn = new THREE.WebGLRenderer({ antialias: true });
    rn.setPixelRatio(Math.min(devicePixelRatio, 2)); rn.setSize(W, H, false); el.appendChild(rn.domElement);
    if (!rn.getContext()) throw new Error("nowebgl");
    webgl = true;
    globe = new THREE.Group(); sc.add(globe);
    // 星空：1600 星点，3 档亮度，shader 逐点闪烁
    var sN = 1600, sp = new Float32Array(sN * 3), pha = new Float32Array(sN), sb = new Float32Array(sN);
    for (var i = 0; i < sN; i++) {
      var v = new THREE.Vector3().randomDirection().multiplyScalar(14 + Math.random() * 46);
      sp[i * 3] = v.x; sp[i * 3 + 1] = v.y; sp[i * 3 + 2] = v.z;
      pha[i] = Math.random();
      var r = Math.random(); sb[i] = r < .55 ? .35 : (r < .85 ? .7 : 1);
    }
    var sg = new THREE.BufferGeometry();
    sg.setAttribute("position", new THREE.BufferAttribute(sp, 3));
    sg.setAttribute("aP", new THREE.BufferAttribute(pha, 1));
    sg.setAttribute("aB", new THREE.BufferAttribute(sb, 1));
    sm = new THREE.ShaderMaterial({
      uniforms: { uT: { value: 0 }, uC: { value: new THREE.Color(0xbfd9ff) } },
      transparent: true, depthWrite: false,
      vertexShader: "attribute float aP;attribute float aB;uniform float uT;varying float vP;varying float vB;void main(){vP=aP;vB=aB;vec4 mv=modelViewMatrix*vec4(position,1.0);gl_PointSize=(150.0/-mv.z)*(0.75+0.5*sin(uT*1.2+aP*6.283))*(0.55+aB*1.15);gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform float uT;uniform vec3 uC;varying float vP;varying float vB;void main(){vec2 c=gl_PointCoord-0.5;float d=length(c);float a=smoothstep(0.5,0.08,d)*(0.55+0.45*sin(uT*1.6+vP*6.283))*(0.5+vB*0.75);if(a<0.05)discard;gl_FragColor=vec4(uC,a);}"
    });
    sc.add(new THREE.Points(sg, sm));
    // 地球贴图：NASA 地表（暗化）+ 夜间灯光加法叠加；大气窄光晕
    var tl = new THREE.TextureLoader(); tl.crossOrigin = undefined;
    var baseTex = tl.load("./static/vendor/textures/earth_atmos_2048.jpg");
    var lightTex = tl.load("./static/vendor/textures/earth_lights_2048.png");
    baseTex.anisotropy = 2; lightTex.anisotropy = 2;
    var base = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 40),
      new THREE.MeshBasicMaterial({ map: baseTex, color: 0x77879e, transparent: true, opacity: .95 }));
    base.renderOrder = 0; globe.add(base);
    var lights = new THREE.Mesh(new THREE.SphereGeometry(1.002, 64, 40),
      new THREE.MeshBasicMaterial({ map: lightTex, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false }));
    lights.renderOrder = 1; globe.add(lights);
    var at = new THREE.Mesh(new THREE.SphereGeometry(1.03, 48, 24),
      new THREE.MeshBasicMaterial({ color: 0x3f8fdd, side: THREE.BackSide, transparent: true, opacity: .28, blending: THREE.AdditiveBlending, depthWrite: false }));
    at.renderOrder = 2; globe.add(at);
    // 经纬网格（经线 12 / 纬线 5）
    var garr = [];
    for (var lo = 0; lo < 360; lo += 30) {
      var a = []; for (var la = -90; la <= 90; la += 6) a.push(latLon(la, lo, 1.004));
      for (var k = 0; k < a.length - 1; k++) { garr.push(a[k].x, a[k].y, a[k].z, a[k + 1].x, a[k + 1].y, a[k + 1].z); }
    }
    for (var la2 = -60; la2 <= 60; la2 += 30) {
      var a2 = []; for (var lo2 = 0; lo2 <= 360; lo2 += 6) a2.push(latLon(la2, lo2, 1.004));
      for (var k2 = 0; k2 < a2.length - 1; k2++) { garr.push(a2[k2].x, a2[k2].y, a2[k2].z, a2[k2 + 1].x, a2[k2 + 1].y, a2[k2 + 1].z); }
    }
    var gg = new THREE.BufferGeometry(); gg.setAttribute("position", new THREE.Float32BufferAttribute(garr, 3));
    globe.add(new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0x3d6ea8, transparent: true, opacity: .12 })));
    // 交互：拖拽 + 滚轮 + 惯性 + 空闲自转
    var drag = null, ivx = 0, ivy = 0, auto = performance.now();
    el.addEventListener("pointerdown", function (e) {
      drag = { x: e.clientX, y: e.clientY }; auto = performance.now(); ivx = ivy = 0; el.setPointerCapture(e.pointerId);
    });
    addEventListener("pointermove", function (e) {
      if (!drag) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      globe.rotation.y += dx * .006;
      globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + dy * .006));
      ivx = dx * .006 * .5; ivy = dy * .006 * .5; auto = performance.now();
    });
    addEventListener("pointerup", function () { drag = null; });
    addEventListener("wheel", function (e) {
      e.preventDefault();
      cam.position.z = Math.min(7, Math.max(2.2, cam.position.z * (1 + e.deltaY * .0012)));
      auto = performance.now();
    }, { passive: false });
    // 帧率统计（滚动 6 秒平均）
    var fpsN = 0, fpsT = performance.now(), fpsA = []; window.__fps5 = 0;
    var clkT = new THREE.Clock();
    (function tick() {
      var now = performance.now(), t = clkT.getElapsedTime();
      sm.uniforms.uT.value = t;
      if (drag) { auto = now; }
      else if (ivx || ivy) {
        globe.rotation.y += ivx;
        globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + ivy));
        ivx *= .94; ivy *= .94; auto = now;
      }
      else if (now - auto > 3000) { globe.rotation.y += .003; }
      hot.forEach(function (h) {
        var k = (now * .0008 + h.ph) % 1, s = .3 + k * .95;
        h.ring.scale.set(s, s, 1);
        h.ring.material.opacity = .7 * (1 - k);
        h.ring.quaternion.copy(cam.quaternion);
      });
      lines.forEach(function (L) { var k = (now * .0004 + L.ph) % 1; L.dot.position.copy(L.c.getPointAt(k)); });
      rn.render(sc, cam);
      if (window.__front === undefined || window.__recalcPts) {
        var ndc = hot.map(function (h) {
          return new THREE.Vector3().copy(h.g.position).applyMatrix4(globe.matrixWorld).project(cam);
        });
        window.__front = ndc.filter(function (v) { return Math.abs(v.x) <= 1 && Math.abs(v.y) <= 1 && v.z < 1; }).length;
        var rr = el.getBoundingClientRect();
        window.__pts = ndc.map(function (v) {
          return (rr.left + (v.x + 1) / 2 * rr.width).toFixed(0) + "," + (rr.top + (1 - v.y) / 2 * rr.height).toFixed(0);
        }).join(";");
        window.__recalcPts = false;
      }
      window.__tex = { a: !!(baseTex.image && baseTex.image.complete), l: !!(lightTex.image && lightTex.image.complete) };
      fpsN++;
      if (now - fpsT >= 1000) {
        fpsA.push(fpsN * 1000 / (now - fpsT));
        if (fpsA.length > 6) fpsA.shift();
        window.__fps5 = Math.round(fpsA.reduce(function (a, b) { return a + b; }, 0) / fpsA.length);
        fpsT = now; fpsN = 0;
      }
      requestAnimationFrame(tick);
    })();
    addEventListener("resize", function () {
      var w = el.clientWidth, h = el.clientHeight;
      cam.aspect = w / h; cam.updateProjectionMatrix(); rn.setSize(w, h, false);
    });
    window.__stars = sN; window.__spots = 0; window.__lines = 0; window.__dots = 0;
  } catch (e) {
    window.__err.push(String(e.message || e));
    if (dgl) dgl.classList.add("show");
  }

  // 动态线索点：dots = [{lat,lon,n,stage}]，n 为该国家线索数
  function setDots(dots) {
    if (!webgl) return;
    hot.forEach(function (h) { globe.remove(h.g); });
    lines.forEach(function (L) { globe.remove(L.L); globe.remove(L.dot); });
    hot = []; lines = [];
    dots.forEach(function (d, i) {
      var g = new THREE.Group(); g.position.copy(latLon(d.lat, d.lon, 1.02));
      var rr = 0.045 + Math.min(d.n || 1, 4) * 0.012;
      var col = STAGE_COLOR[d.stage] || 0x4d9de0;
      var core = new THREE.Mesh(new THREE.SphereGeometry(rr, 12, 8), new THREE.MeshBasicMaterial({ color: col }));
      var ring = new THREE.Mesh(new THREE.RingGeometry(rr * .9, rr * 1.9, 32),
        new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: .75, side: THREE.DoubleSide, blending: THREE.AdditiveBlending, depthWrite: false }));
      g.add(core, ring); globe.add(g);
      hot.push({ ring: ring, ph: (i * .17) % 1, g: g });
    });
    // 飞线：从线索最多国家到其余（≤3 条）
    var sorted = dots.slice().sort(function (a, b) { return (b.n || 1) - (a.n || 1); });
    if (sorted.length >= 2) {
      var from = sorted[0];
      sorted.slice(1, 4).forEach(function (to, i) {
        var pa = latLon(from.lat, from.lon, 1.04), pb = latLon(to.lat, to.lon, 1.04);
        var mid = pa.clone().add(pb).multiplyScalar(.5).normalize().multiplyScalar(1.28);
        var c = new THREE.QuadraticBezierCurve3(pa, mid, pb);
        var L = new THREE.Line(new THREE.BufferGeometry().setFromPoints(c.getPoints(56)),
          new THREE.LineBasicMaterial({ color: [0x7fd8ff, 0x8fb8ff, 0xc9a8ff][i % 3], transparent: true, opacity: .9, blending: THREE.AdditiveBlending }));
        var dot = new THREE.Mesh(new THREE.SphereGeometry(.022, 8, 8), new THREE.MeshBasicMaterial({ color: 0xd8f2ff }));
        globe.add(L, dot);
        lines.push({ c: c, dot: dot, L: L, ph: i * .31 });
      });
    }
    window.__spots = hot.length; window.__lines = lines.length; window.__dots = hot.length;
    window.__front = undefined; window.__recalcPts = true;
  }

  window.EARTH = { setDots: setDots, ok: function () { return webgl; } };
})();
