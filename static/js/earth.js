/* 3D 地球模块 V2：L0 三层星空 / L1 昼夜 Shader 地球 / L2 双层大气 / L3 经纬网格 / L4 潜力光点(Sprite) / L5 贝塞尔飞线。
   依赖 static/vendor/three.min.js（r149 UMD，无 EffectComposer/Bloom，辉光全用加法混合 + 代码生成贴图）。
   输入：window.EARTH.setDots([{lat,lon,n,stage}])，国家/潜力/id 从 window.__leads 匹配补齐。
   输出：window.EARTH.ok()、window.__fps5/__spots/__lines/__dots/__sun/__fallback 等验收用调试量。
   回退：WebGL 或 Shader 编译失败 → MeshBasicMaterial 贴图球 + 控制台警告，绝不留黑屏。 */
(function () {
  var $ = function (i) { return document.getElementById(i); };
  window.__err = window.__err || [];
  var el = $("g3d"), dgl = $("dgl");
  var webgl = false, globe, cam, rn, scene;
  var hot = [], lines = [];
  var STAGE_COLOR = { S1: 0x4d9de0, S2: 0x8a63f5, S3: 0xf5a623, S4: 0x2ecc8f };
  var STAGE_NAME = { S1: "S1 选品", S2: "S2 供应链", S3: "S3 交易结构", S4: "S4 客户开发" };
  var MAX_DOTS = 30, MAX_LINES = 12, MAX_STARS = 3000;

  function latLon(lat, lon, r) {
    var a = lat * Math.PI / 180, b = lon * Math.PI / 180;
    return new THREE.Vector3(Math.cos(a) * Math.sin(b) * r, Math.sin(a) * r, Math.cos(a) * Math.cos(b) * r);
  }
  // 计算太阳方向单位向量（真实 UTC 时间，每 60s 更新；失败回退固定向量）
  function computeSun() {
    var now = new Date();
    var day = (Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - Date.UTC(now.getUTCFullYear(), 0, 0)) / 864e5;
    var gamma = 2 * Math.PI / 365 * (day - 1 + (now.getUTCHours() - 12) / 24);
    var decl = 0.006918 - 0.399912 * Math.cos(gamma) + 0.070257 * Math.sin(gamma);
    var eqTime = 229.18 * (0.000075 + 0.001868 * Math.cos(gamma) - 0.032077 * Math.sin(gamma) - 0.014615 * Math.cos(2 * gamma) - 0.040849 * Math.sin(2 * gamma)) / 60;
    var ha = (now.getUTCHours() + now.getUTCMinutes() / 60 - 12 + eqTime) / 24 * 2 * Math.PI;
    var latR = decl, lonR = -ha;
    return new THREE.Vector3(Math.cos(latR) * Math.sin(lonR), Math.sin(latR), Math.cos(latR) * Math.cos(lonR)).normalize();
  }
  // 覆盖太阳方向：?sun=x,y,z（调试）
  function parseSunOverride() {
    try {
      var m = (location.search || "").match(/[?&]sun=([^&]+)/);
      if (m) {
        var p = m[1].split(",").map(parseFloat);
        if (p.length === 3 && p.every(function (v) { return isFinite(v); })) {
          var v = new THREE.Vector3(p[0], p[1], p[2]);
          if (v.lengthSq() > 0.001) { v.normalize(); window.__sunOverride = v.toArray(); return v; }
        }
      }
    } catch (e) { /* 参数非法时忽略 */ }
    return null;
  }
  var sunOverride = parseSunOverride();
  var sunDir = sunOverride || computeSun();
  var sunTimer = 0;
  function refreshSun() {
    if (!sunOverride) sunDir = computeSun();
    if (baseMat) { baseMat.uniforms.uSun.value.copy(sunDir); }
    window.__sun = sunDir.toArray();
  }

  // ---- 代码生成径向渐变发光贴图（光点/光晕共用）----
  var glowTex = null;
  function buildGlowTex() {
    var c = document.createElement("canvas"); c.width = c.height = 64;
    var g = c.getContext("2d");
    var grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, "rgba(255,255,255,0.85)");
    grad.addColorStop(0.25, "rgba(255,255,255,0.62)");
    grad.addColorStop(0.55, "rgba(255,255,255,0.20)");
    grad.addColorStop(1, "rgba(255,255,255,0)");
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    glowTex = new THREE.CanvasTexture(c);
  }

  // 按阶段色生成光点贴图（缓存 4 份），避免白色核心盖掉阶段色
  var glowCache = {};
  function glowFor(hex) {
    var key = "#" + new THREE.Color(hex).getHexString();
    if (glowCache[key]) return glowCache[key];
    var c = document.createElement("canvas"); c.width = c.height = 64;
    var g = c.getContext("2d");
    var rgb = new THREE.Color(hex);
    var r = Math.round(rgb.r * 255), gg2 = Math.round(rgb.g * 255), b = Math.round(rgb.b * 255);
    var grad = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    grad.addColorStop(0, "rgba(" + r + "," + gg2 + "," + b + ",1)");
    grad.addColorStop(0.30, "rgba(" + r + "," + gg2 + "," + b + ",0.72)");
    grad.addColorStop(0.62, "rgba(" + r + "," + gg2 + "," + b + ",0.22)");
    grad.addColorStop(1, "rgba(" + r + "," + gg2 + "," + b + ",0)");
    g.fillStyle = grad; g.fillRect(0, 0, 64, 64);
    var t = new THREE.CanvasTexture(c);
    glowCache[key] = t;
    return t;
  }

  try {
    if (!document.createElement("canvas").getContext("webgl") &&
        !document.createElement("canvas").getContext("experimental-webgl")) throw new Error("nowebgl");
    var W = el.clientWidth || 800, H = el.clientHeight || 400;
    scene = new THREE.Scene(); scene.background = new THREE.Color(0x060a12);
    cam = new THREE.PerspectiveCamera(45, W / H, 0.1, 300); cam.position.set(0, 0, 4);
    rn = new THREE.WebGLRenderer({ antialias: true });
    rn.setPixelRatio(Math.min(devicePixelRatio, 2)); rn.setSize(W, H, false);
    if (!rn.getContext()) throw new Error("nowebgl");
    webgl = true;
    el.appendChild(rn.domElement);
    var cv = rn.domElement;

    globe = new THREE.Group(); globe.position.x = -0.10;
    // 初始自转：让晨昏线横跨可见面（太阳在 +Z 附近时，-90° 使夜面/城市灯光可见）
    globe.rotation.y = -Math.PI / 2;
    scene.add(globe);
    var tl = new THREE.TextureLoader(); tl.crossOrigin = undefined;
    var baseTex = tl.load("./static/vendor/textures/earth_atmos_2048.jpg");
    var lightTex = tl.load("./static/vendor/textures/earth_lights_2048.png");
    baseTex.anisotropy = 2; lightTex.anisotropy = 2;

    // ---- L0 星空：三层 Points（近/中/远，共 1200–3000 点），顶点着色器闪烁 + 极慢自转 ----
    var starsLayer = [];
    function buildStars(count, r0, r1, sizeBase, bright) {
      var pos = new Float32Array(count * 3), pha = new Float32Array(count), br = new Float32Array(count);
      for (var i = 0; i < count; i++) {
        var v = new THREE.Vector3().randomDirection().multiplyScalar(r0 + Math.random() * (r1 - r0));
        pos[i * 3] = v.x; pos[i * 3 + 1] = v.y; pos[i * 3 + 2] = v.z;
        pha[i] = Math.random();
        var r = Math.random();
        br[i] = r < 0.55 ? 0.35 : (r < 0.85 ? 0.7 : 1);
      }
      var g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      g.setAttribute("aP", new THREE.BufferAttribute(pha, 1));
      g.setAttribute("aB", new THREE.BufferAttribute(br, 1));
      var m = new THREE.ShaderMaterial({
        uniforms: { uT: { value: 0 }, uC: { value: new THREE.Color(0xbfd9ff) }, uSize: { value: sizeBase }, uOp: { value: bright } },
        transparent: true, depthWrite: false,
        vertexShader: "attribute float aP;attribute float aB;uniform float uT;uniform float uSize;varying float vP;varying float vB;void main(){vP=aP;vB=aB;vec4 mv=modelViewMatrix*vec4(position,1.0);gl_PointSize=(uSize/-mv.z)*(0.75+0.5*sin(uT*1.2+aP*6.283))*(0.55+aB*1.15);gl_Position=projectionMatrix*mv;}",
        fragmentShader: "uniform float uT;uniform vec3 uC;uniform float uOp;varying float vP;varying float vB;void main(){vec2 c=gl_PointCoord-0.5;float d=length(c);float a=smoothstep(0.5,0.08,d)*(0.55+0.45*sin(uT*1.6+vP*6.283))*(0.5+vB*0.75)*uOp;if(a<0.05)discard;gl_FragColor=vec4(uC,a);}"
      });
      var p = new THREE.Points(g, m);
      scene.add(p);
      starsLayer.push({ obj: p, mat: m });
    }
    // 近 700 / 中 800 / 远 500 = 2000 点（≤3000，≥1200）
    buildStars(700, 13, 20, 150, 1.0);
    buildStars(800, 22, 34, 90, 0.62);
    buildStars(500, 38, 60, 50, 0.34);

    // ---- L1 地球：昼夜混合 ShaderMaterial（日间贴图×太阳照度 + 夜间灯光×暗面权重，smoothstep 软化晨昏线）----
    var baseMat = new THREE.ShaderMaterial({
      uniforms: {
        uSun: { value: sunDir.clone() },
        uDay: { value: new THREE.Texture() },
        uNight: { value: new THREE.Texture() },
        uDayMul: { value: 1.15 }, uNightMul: { value: 2.10 }, uAmb: { value: 0.12 },
        uTint: { value: new THREE.Color(0x9fb8d8) }
      },
      vertexShader: "varying vec3 vN;varying vec2 vUv;varying vec3 vV;void main(){vUv=uv;vN=normalize((modelMatrix*vec4(normal,0.0)).xyz);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=-mv.xyz;gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform vec3 uSun;uniform sampler2D uDay;uniform sampler2D uNight;uniform float uDayMul;uniform float uNightMul;uniform float uAmb;uniform vec3 uTint;varying vec3 vN;varying vec2 vUv;varying vec3 vV;void main(){vec3 N=normalize(vN);float s=dot(N,uSun);float dayF=smoothstep(-0.07,0.07,s);float nightF=smoothstep(0.15,-0.15,s);vec3 day=texture2D(uDay,vUv).rgb*max(uDayMul*s,0.0);vec3 night=texture2D(uNight,vUv).rgb*uNightMul*nightF;vec3 col=uTint*(uAmb+day)+night;col*=0.86+0.14*pow(1.0-max(dot(N,normalize(vV)),0.0),2.5);gl_FragColor=vec4(col,1.0);}"
    });
    baseMat.uniforms.uDay.value = baseTex;
    baseMat.uniforms.uNight.value = lightTex;
    var base = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 40), baseMat);
    base.renderOrder = 0; globe.add(base);
    refreshSun();

    // 经纬网格（L3，透明度降到 0.06，仅空间参照）
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
    var grid = new THREE.LineSegments(gg, new THREE.LineBasicMaterial({ color: 0x3d6ea8, transparent: true, opacity: 0.06 }));
    grid.renderOrder = 1; globe.add(grid);

    // ---- L2 大气：内层 BackSide 菲涅尔加法辉光 + 外层半透明外晕 ----
    var atmMat = new THREE.ShaderMaterial({
      uniforms: { uCol: { value: new THREE.Color(0x3f8fdd) }, uPow: { value: 2.6 }, uK: { value: 0.45 } },
      side: THREE.BackSide, transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: "varying vec3 vN;varying vec3 vV;void main(){vN=normalize((modelMatrix*vec4(normal,0.0)).xyz);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform vec3 uCol;uniform float uPow;uniform float uK;varying vec3 vN;varying vec3 vV;void main(){float f=pow(1.0-max(dot(normalize(vN),normalize(vV)),0.0),uPow)*uK;gl_FragColor=vec4(uCol*f,f*0.85);}"
    });
    var atm = new THREE.Mesh(new THREE.SphereGeometry(1.03, 48, 24), atmMat);
    atm.renderOrder = 2; globe.add(atm);
    var haloMat = new THREE.ShaderMaterial({
      uniforms: { uCol: { value: new THREE.Color(0x3f8fdd) }, uPow: { value: 3.0 }, uK: { value: 0.20 } },
      transparent: true, blending: THREE.AdditiveBlending, depthWrite: false,
      vertexShader: "varying vec3 vN;varying vec3 vV;void main(){vN=normalize((modelMatrix*vec4(normal,0.0)).xyz);vec4 mv=modelViewMatrix*vec4(position,1.0);vV=normalize(-mv.xyz);gl_Position=projectionMatrix*mv;}",
      fragmentShader: "uniform vec3 uCol;uniform float uPow;uniform float uK;varying vec3 vN;varying vec3 vV;void main(){float f=smoothstep(0.0,0.14,dot(normalize(vN),normalize(vV)));f=1.0-f;f=pow(f,uPow)*uK;gl_FragColor=vec4(uCol*f,f);}"
    });
    var halo = new THREE.Mesh(new THREE.SphereGeometry(1.16, 48, 24), haloMat);
    halo.renderOrder = 2; globe.add(halo);

    buildGlowTex();

    // ---- 光点标签（悬停显示「国家 · 阶段 · 潜力」）----
    var label = document.createElement("div");
    label.className = "g3d-label";
    el.appendChild(label);
    function showLabel(h, x, y) {
      label.style.left = x + "px"; label.style.top = y + "px";
      label.style.borderColor = "#" + new THREE.Color(STAGE_COLOR[h.stage] || STAGE_COLOR.S1).getHexString();
      label.textContent = (h.country || "—") + " · " + (STAGE_NAME[h.stage] || h.stage) + " · 潜力 " + (h.pot == null ? "—" : h.pot);
      label.classList.add("show");
    }
    function hideLabel() { label.classList.remove("show"); }

    // ---- 交互：拖拽旋转 / 滚轮缩放（仅地球区）/ 悬停放大+标签 / 点击（位移<4px）----
    var drag = null, ivx = 0, ivy = 0, auto = performance.now();
    var pointer = { x: -9999, y: -9999 }, hover = null, downPt = null;
    var raycaster = new THREE.Raycaster(); raycaster.params.Sprite = { threshold: 0.02 };

    cv.addEventListener("pointerdown", function (e) {
      downPt = { x: e.clientX, y: e.clientY };
      drag = { x: e.clientX, y: e.clientY };
      auto = performance.now(); ivx = ivy = 0;
      cv.setPointerCapture(e.pointerId);
      e.stopPropagation();
    });
    document.addEventListener("pointermove", function (e) {
      pointer.x = e.clientX; pointer.y = e.clientY;
      if (!drag) return;
      var dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      drag.x = e.clientX; drag.y = e.clientY;
      globe.rotation.y += dx * 0.006;
      globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + dy * 0.006));
      ivx = dx * 0.006 * 0.5; ivy = dy * 0.006 * 0.5; auto = performance.now();
      e.stopPropagation();
    });
    document.addEventListener("pointerup", function (e) {
      if (downPt && Math.abs(e.clientX - downPt.x) < 4 && Math.abs(e.clientY - downPt.y) < 4) {
        // 点击：命中光点则打开详情页（新标签），点击空白取消
        var h = hitTest(pointer.x, pointer.y);
        if (h) {
          if (h.hover === undefined) { h = null; }
          else if (h.id) window.open("/static/detail.html?id=" + h.id, "_blank");
        }
      }
      if (hover && !h && h !== undefined) { hover.hover = false; hover.spr.scale.setScalar(hover.base); hover = null; hideLabel(); }
      drag = null; downPt = null;
    });
    cv.addEventListener("wheel", function (e) {
      if (!el.contains(e.target)) return;
      e.preventDefault();
      cam.position.z = Math.min(7, Math.max(2.2, cam.position.z * (1 + e.deltaY * 0.0012)));
      auto = performance.now();
    }, { passive: false });
    cv.addEventListener("mouseleave", function () {
      pointer.x = -9999; pointer.y = -9999;
      if (hover) { hover.hover = false; hover.spr.scale.setScalar(hover.base); hover = null; }
      hideLabel();
    });

    var hoverTick = 0;
    var HIT_SCALE = 2.6; // 命中区域放大，方便点击/悬停小光点
    function hitTest(cx, cy) {
      if (!hot.length || !cv) return null;
      var rr = cv.getBoundingClientRect();
      raycaster.setFromCamera(new THREE.Vector2((cx - rr.left) / rr.width * 2 - 1, -(cy - rr.top) / rr.height * 2 + 1), cam);
      hot.forEach(function (h) { h.spr.scale.setScalar(h.base * HIT_SCALE); });
      var hit = raycaster.intersectObjects(hot.map(function (h) { return h.spr; }), false);
      hot.forEach(function (h) { h.spr.scale.setScalar(h.base); });
      return hit.length ? hit[0].object.userData.h : null;
    }

    // 帧率统计 + 太阳 60s 刷新
    var fpsN = 0, fpsT = performance.now(), fpsA = []; window.__fps5 = 0;
    var clkT = new THREE.Clock();
    var sunRefreshMs = 60000;
    (function tick() {
      var now = performance.now(), t = clkT.getElapsedTime();
      starsLayer.forEach(function (s) { s.mat.uniforms.uT.value = t; });
      starsLayer.forEach(function (s) { s.obj.rotation.y += 0.00006; });
      if (now - sunTimer >= sunRefreshMs) { sunTimer = now; refreshSun(); }

      if (drag) { auto = now; }
      else if (ivx || ivy) {
        globe.rotation.y += ivx;
        globe.rotation.x = Math.max(-1.3, Math.min(1.3, globe.rotation.x + ivy));
        ivx *= 0.94; ivy *= 0.94; auto = now;
      }
      else if (now - auto > 3000 && !window.__stopAuto) { globe.rotation.y += 0.003; }

      // 光点呼吸 + 脉冲环（呼吸缩放作用于 g，环在 g 内抵消世界缩放）
      hot.forEach(function (h) {
        var br = h.base * (1 + 0.10 * Math.sin(t * 2.1 + h.ph * 6.283));
        h.g.scale.setScalar(br);
        h.spr.scale.setScalar(h.hover ? h.base * 1.3 : h.base);
        var k = (now * 0.0008 + h.ph) % 1, s = 0.3 + k * 0.95;
        h.ring.scale.set(s / br, s / br, 1);
        h.ring.material.opacity = 0.7 * (1 - k);
        h.ring.quaternion.copy(cam.quaternion);
      });
      lines.forEach(function (L) {
        var k = (now * 0.0004 + L.ph) % 1;
        L.dot.position.copy(L.c.getPointAt(k));
        if (L.glow) L.glow.position.copy(L.c.getPointAt(0));
      });

      // 悬停检测（节流 ~每 4 帧）
      if (++hoverTick % 4 === 0 && !drag) {
        var h2 = hitTest(pointer.x, pointer.y);
        if (h2 !== hover) {
          if (hover) { hover.hover = false; hover.spr.scale.setScalar(hover.base); }
          hover = h2;
          if (hover) {
            hover.hover = true;
            hover.spr.scale.setScalar(hover.base * 1.3);
            var p = new THREE.Vector3().copy(hover.g.position).applyMatrix4(globe.matrixWorld).project(cam);
            var rr2 = cv.getBoundingClientRect();
            showLabel(hover, rr2.left + (p.x + 1) / 2 * rr2.width, rr2.top + (1 - p.y) / 2 * rr2.height);
          } else hideLabel();
        }
      }

      if (cv.clientWidth > 0 && cv.clientHeight > 0) rn.render(scene, cam);

      if (window.__front === undefined || window.__recalcPts || window.__debugPts) {
        var ndc = hot.map(function (h) {
          return new THREE.Vector3().copy(h.g.position).applyMatrix4(globe.matrixWorld).project(cam);
        });
        window.__front = ndc.filter(function (v) { return Math.abs(v.x) <= 1 && Math.abs(v.y) <= 1 && v.z < 1; }).length;
        var rr = cv.getBoundingClientRect();
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
      cam.aspect = w / h; cam.updateProjectionMatrix();
      rn.setPixelRatio(Math.min(devicePixelRatio, 2)); rn.setSize(w, h, false);
    });

    // ---- Shader 编译失败回退：MeshBasicMaterial 贴图球 + 警告，绝不留黑屏 ----
    try {
      rn.render(scene, cam);
    } catch (e) {
      window.__err.push("shader-fallback:" + String(e.message || e));
      console.warn("[earth-v2] Shader 编译失败，回退 MeshBasicMaterial 贴图球：", e);
      [base, atm, halo].forEach(function (m) { if (m) { globe.remove(m); m.material.dispose(); } });
      var fb = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 40),
        new THREE.MeshBasicMaterial({ map: baseTex, color: 0x9fb8d8, transparent: true, opacity: 0.95 }));
      fb.renderOrder = 0;
      globe.add(fb);
      baseMat = null;
      window.__fallback = true;
    }

    window.__stars = starsLayer.reduce(function (a, s) { return a + s.obj.geometry.attributes.position.count; }, 0);
    window.__spots = 0; window.__lines = 0; window.__dots = 0;
    // 验收/调试钩子：返回活引用
    window.__dbg = {
      globe: function () { return globe; }, cam: function () { return cam; }, rn: function () { return rn; },
      hot: function () { return hot; }, lines: function () { return lines; },
      hitTest: function (x, y) { return hitTest(x, y); }, label: function () { return label; }
    };
  } catch (e) {
    window.__err.push(String(e.message || e));
    if (dgl) dgl.classList.add("show");
  }

  // ---- 动态线索点 + 飞线（dots=[{lat,lon,n,stage}]，country/pot/id 从 window.__leads 补齐）----
  function setDots(dots) {
    if (!webgl || !globe) return;
    hot.forEach(function (h) { globe.remove(h.g); });
    lines.forEach(function (L) { globe.remove(L.L); globe.remove(L.dot); if (L.glow) globe.remove(L.glow); });
    hot = []; lines = [];

    // 从 __leads 构建 国家→(pot 取最大, 代表 id)
    var leads = window.__leads || [];
    var lm = {};
    leads.forEach(function (l) {
      if (!l || !l.country) return;
      var e = lm[l.country] || { pot: -1, id: null };
      var p = Number(l.potential) || 0;
      if (p > e.pot) { e.pot = p; e.id = l.id; }
      lm[l.country] = e;
    });

    var arr = dots.slice(0, MAX_DOTS);
    arr.forEach(function (d, i) {
      var g = new THREE.Group(); g.position.copy(latLon(d.lat, d.lon, 1.02));
      var col = STAGE_COLOR[d.stage] || 0x4d9de0;
      var meta = lm[d.country] || { pot: null, id: null };
      var pot = meta.pot == null ? (d.pot != null ? d.pot : null) : meta.pot;
      var size = 0.6 + 0.8 * ((pot == null ? 0 : Math.min(pot, 100)) / 100); // 0.6–1.4 倍
      var baseS = 0.060 * size;
      var entry = { g: g, ring: null, base: baseS, ph: (i * 0.17) % 1, country: d.country, stage: d.stage, pot: pot, id: meta.id, hover: false };
      var spr = new THREE.Sprite(new THREE.SpriteMaterial({
        map: glowFor(col), color: 0xffffff, transparent: true, opacity: 0.95,
        blending: THREE.AdditiveBlending, depthWrite: false
      }));
      spr.scale.setScalar(baseS);
      spr.renderOrder = 4;
      spr.userData.h = entry;
      var ring = new THREE.Mesh(new THREE.RingGeometry(baseS * 0.9, baseS * 1.9, 32),
        new THREE.MeshBasicMaterial({ color: col, transparent: true, opacity: 0.75, side: THREE.DoubleSide, blending: THREE.AdditiveBlending, depthWrite: false }));
      ring.renderOrder = 4;
      entry.spr = spr; entry.ring = ring;
      g.add(spr, ring); globe.add(g);
      hot.push(entry);
    });

    // 飞线：潜力最高 → 其余（三次贝塞尔弧，最多 12 条，按潜力截断）
    var ordered = arr.slice().map(function (d, i) {
      return { d: d, pot: lm[d.country] ? lm[d.country].pot : (d.pot != null ? d.pot : 0) };
    }).sort(function (a, b) { return (b.pot || 0) - (a.pot || 0); });
    var from = ordered[0];
    if (ordered.length >= 2) {
      var tos = ordered.slice(1, MAX_LINES + 1);
      tos.forEach(function (to, i) {
        var pa = latLon(from.d.lat, from.d.lon, 1.0), pb = latLon(to.d.lat, to.d.lon, 1.0);
        var mid = pa.clone().add(pb).multiplyScalar(0.5).normalize().multiplyScalar(1.3);
        var c = new THREE.CubicBezierCurve3(pa.clone().multiplyScalar(1.02), mid, mid, pb.clone().multiplyScalar(1.02));
        var L = new THREE.Line(new THREE.BufferGeometry().setFromPoints(c.getPoints(48)),
          new THREE.LineBasicMaterial({ color: [0x7fd8ff, 0x8fb8ff, 0xc9a8ff][i % 3], transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending, depthWrite: false }));
        L.renderOrder = 3;
        var dot = new THREE.Mesh(new THREE.SphereGeometry(0.022, 8, 8),
          new THREE.MeshBasicMaterial({ color: 0xd8f2ff, blending: THREE.AdditiveBlending, depthWrite: false }));
        dot.renderOrder = 4;
        var glow = new THREE.Sprite(new THREE.SpriteMaterial({
          map: glowTex, color: 0x7fd8ff, transparent: true, opacity: 0.6,
          blending: THREE.AdditiveBlending, depthWrite: false
        }));
        glow.scale.setScalar(0.09); glow.renderOrder = 3;
        globe.add(L, dot, glow);
        lines.push({ c: c, dot: dot, L: L, glow: glow, ph: i * 0.31 });
      });
    }
    window.__spots = hot.length; window.__lines = lines.length; window.__dots = hot.length;
    window.__front = undefined; window.__recalcPts = true;
  }

  window.EARTH = {
    setDots: setDots,
    ok: function () { return webgl; },
    sun: function () { return sunDir.toArray(); }
  };
})();
